"""
Terminal service — PTY lifecycle management with keepalive support.

Manages pseudo-terminal sessions for the integrated terminal feature.
Each session is backed by a ``subprocess.Popen`` connected to a PTY
created via ``pty.openpty()``.

A dedicated **reader thread** continuously reads from the PTY master fd
and pushes output into both an ``OutputBuffer`` (for replay on
reconnection) and an ``asyncio.Queue`` (for live WebSocket relay).

Session states:
    ACTIVE   — WS connected, live relay running
    ORPHANED — WS disconnected, session kept alive for grace period

A background **reaper task** periodically kills expired orphaned sessions
and sessions whose PTY process has exited.
"""

import asyncio
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.utils import generate_id

logger = get_logger(__name__)

_READ_SIZE = 65536
_REAPER_INTERVAL = 30  # seconds


# ------------------------------------------------------------------
# Session state
# ------------------------------------------------------------------

class SessionState(Enum):
    ACTIVE = auto()    # WS connected, output flows to WS
    ORPHANED = auto()  # WS disconnected, within grace period


# ------------------------------------------------------------------
# OutputBuffer — thread-safe bounded byte buffer for replay
# ------------------------------------------------------------------

class OutputBuffer:
    """Thread-safe bounded byte buffer for PTY output replay.

    Stores up to ``MAX_BYTES`` of the most recent terminal output using
    a deque of byte chunks.  When the cap is exceeded, oldest chunks are
    discarded one at a time — no large bytearray copies.

    Supports a *pause window*: after a PTY resize the shell may redraw its
    prompt using ``\\r\\x1b[K`` escape sequences.  These are correct in a
    live stream but produce duplicate lines when replayed.  Setting
    ``pause_until`` causes ``write()`` to skip buffering for a short
    period — data still reaches the live queue, just not the replay buffer.
    """

    MAX_BYTES = 5 * 1024 * 1024  # 5 MB

    def __init__(self) -> None:
        self._chunks: deque[bytes] = deque()
        self._size: int = 0
        self._lock: threading.Lock = threading.Lock()
        self.pause_until: float = 0.0  # 0 = not paused

    def write(self, data: bytes) -> None:
        """Append data.  Called from the reader thread.

        Skips buffering when inside a pause window (resize redraw guard).
        """
        if time.monotonic() < self.pause_until:
            return
        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            while self._size > self.MAX_BYTES:
                removed = self._chunks.popleft()
                self._size -= len(removed)

    def snapshot(self) -> bytes:
        """Atomically snapshot all buffered data.

        Returns a concatenated byte string of the current terminal history.
        Does NOT clear the buffer, allowing multiple clients or rapid
        reconnections to receive the same history.
        """
        with self._lock:
            return b"".join(self._chunks)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._size = 0


# ------------------------------------------------------------------
# TerminalSession
# ------------------------------------------------------------------

@dataclass
class TerminalSession:
    """Represents a single PTY session."""

    session_id: str
    master_fd: int
    process: subprocess.Popen
    project_path: str
    project_id: str
    _output_queue: asyncio.Queue = field(repr=False)
    terminal_slot: int = 1
    state: SessionState = field(default=SessionState.ACTIVE)
    orphaned_at: float | None = None
    output_buffer: OutputBuffer = field(default_factory=OutputBuffer, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _ws_generation: int = field(default=0, repr=False)
    _ws: Any = field(default=None, repr=False)


# ------------------------------------------------------------------
# TerminalService
# ------------------------------------------------------------------

class TerminalService:
    """Singleton service that manages all active PTY sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> TerminalSession | None:
        """Return a session by ID, or None if not found."""
        return self._sessions.get(session_id)

    async def _create_session(
        self,
        project_id: str,
        cols: int = 80,
        rows: int = 24,
        terminal_slot: int = 1,
    ) -> TerminalSession:
        """Create a new PTY session for *project_id*.

        Assumes ``self._lock`` is held by the caller — the session-count
        check and the dict insertion must be atomic to prevent two
        concurrent ``find_or_claim_session`` calls from each creating a
        session for the same (project_id, slot) pair.
        """
        project_path = settings.get_project_path(project_id).resolve()
        if not project_path.exists():
            raise FileNotFoundError(f"Project directory not found: {project_path}")

        # ── Enforce session limit ──
        active_count = sum(
            1 for s in self._sessions.values()
            if s.project_id == project_id
        )
        if active_count >= settings.TERMINAL_MAX_SESSIONS:
            raise RuntimeError(
                f"Maximum terminal sessions ({settings.TERMINAL_MAX_SESSIONS}) reached for project {project_id}"
            )

        session_id = generate_id()

        # ── Create PTY pair ──
        master_fd, slave_fd = pty.openpty()

        # Set initial terminal size on the slave before spawning
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        # Also set on master so the size is immediately consistent
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        # ── Spawn shell ──
        shell = os.environ.get("SHELL", "/bin/bash")
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)

        try:
            process = subprocess.Popen(
                [shell, "--login"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(project_path),
                env=env,
                start_new_session=True,  # new process group for clean kill
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise

        # Close slave fd in parent — child has inherited it
        os.close(slave_fd)

        logger.info(
            "Terminal session %s created (pid=%d, shell=%s, cwd=%s, slot=%d)",
            session_id[:8], process.pid, shell, project_path, terminal_slot,
        )

        # ── Build session ──
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=4096)
        stop_event = threading.Event()

        session = TerminalSession(
            session_id=session_id,
            master_fd=master_fd,
            process=process,
            project_path=str(project_path),
            project_id=project_id,
            terminal_slot=terminal_slot,
            _output_queue=queue,
            _stop_event=stop_event,
        )

        # ── Start reader thread ──
        loop = asyncio.get_running_loop()
        thread = threading.Thread(
            target=_reader_thread_func,
            args=(master_fd, queue, stop_event, loop, session_id[:8], session.output_buffer),
            daemon=True,
            name=f"pty-reader-{session_id[:8]}",
        )
        session._reader_thread = thread
        thread.start()

        self._sessions[session_id] = session
        return session

    # ------------------------------------------------------------------
    # Session acquisition (the single entry point for all callers)
    # ------------------------------------------------------------------

    def _find_by_slot(self, project_id: str, slot: int) -> TerminalSession | None:
        """Return any session matching *project_id* and *slot*, or None."""
        for s in self._sessions.values():
            if s.project_id == project_id and s.terminal_slot == slot:
                return s
        return None

    async def find_or_claim_session(
        self,
        project_id: str,
        slot: int,
        cols: int = 80,
        rows: int = 24,
    ) -> tuple[TerminalSession, bool]:
        """Acquire the terminal session for *project_id* + *slot*.

        Three cases, handled atomically from the caller's perspective:

        1. **No existing session** → create a new one.
        2. **Existing ORPHANED session** → reattach.
        3. **Existing ACTIVE session** → takeover.

        Returns ``(session, is_reattach)``.  *is_reattach* is ``True`` when
        an existing session was claimed (cases 2 & 3); the caller MUST take
        the replay snapshot AFTER processing any pending resize / input
        messages to avoid corrupting the terminal display.

        The caller MUST capture ``session._ws_generation`` immediately
        after this call returns and check it in its ``finally`` block —
        a mismatch means the session was taken over and the handler
        must not touch it.
        """
        async with self._lock:
            existing = self._find_by_slot(project_id, slot)

            if existing is None:
                # ── Case 1: brand-new session ──
                session = await self._create_session(project_id, cols, rows, terminal_slot=slot)
                return (session, False)

            if existing.state == SessionState.ACTIVE:
                # ── Case 3: takeover stale ACTIVE session ──
                existing._ws_generation += 1
                if existing._ws is not None:
                    try:
                        await existing._ws.close(code=4001, reason="taken over")
                    except Exception:
                        logger.debug("Failed to close stale terminal websocket", exc_info=True)
                    existing._ws = None

            # ── Case 2 + 3: reattach (or takeover-then-reattach) ──
            if existing.process.poll() is not None:
                # Process died while orphaned — replace with fresh session
                await self._kill_session_unlocked(existing.session_id)
                session = await self._create_session(project_id, cols, rows, terminal_slot=slot)
                return (session, False)

            existing.state = SessionState.ACTIVE
            existing.orphaned_at = None

            # Drain the live queue so that anything already in the replay buffer
            # isn't duplicated in the live stream immediately after reattach.
            while not existing._output_queue.empty():
                try:
                    existing._output_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            logger.info(
                "Session %s claimed (slot=%d, gen=%d)",
                existing.session_id[:8], slot, existing._ws_generation,
            )
            return (existing, True)

    def list_project_sessions(self, project_id: str) -> list[dict]:
        """Return metadata for every non-dead session in *project_id*."""
        result: list[dict] = []
        for s in self._sessions.values():
            if s.project_id == project_id and s.state in (SessionState.ACTIVE, SessionState.ORPHANED):
                result.append({
                    "session_id": s.session_id,
                    "slot": s.terminal_slot,
                    "state": s.state.name.lower(),
                })
        result.sort(key=lambda x: x["slot"])
        return result

    async def detach_session(self, session_id: str) -> None:
        """Mark session as orphaned without killing the PTY process.

        If the process has already exited, kills the session immediately
        instead of orphaning.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        # If process already exited, kill immediately
        if session.process.poll() is not None:
            await self.kill_session(session_id)
            return
        session.state = SessionState.ORPHANED
        session.orphaned_at = time.monotonic()
        logger.info(
            "Session %s detached (orphaned), grace period started",
            session_id[:8],
        )

    # ------------------------------------------------------------------
    # Reaper
    # ------------------------------------------------------------------

    async def start_reaper(self) -> None:
        """Start the background reaper task (called once at startup)."""
        self._reaper_task = asyncio.create_task(self._reaper_loop())
        logger.info("Terminal session reaper started")

    async def _reaper_loop(self) -> None:
        """Periodically kill expired orphaned sessions and dead processes."""
        while True:
            await asyncio.sleep(_REAPER_INTERVAL)
            async with self._lock:
                now = time.monotonic()
                to_kill: list[str] = []
                for sid, session in self._sessions.items():
                    # Kill sessions whose process has exited
                    if session.process.poll() is not None:
                        to_kill.append(sid)
                    # Kill orphaned sessions past grace period
                    elif (session.state == SessionState.ORPHANED
                          and session.orphaned_at is not None
                          and (now - session.orphaned_at) > settings.TERMINAL_GRACE_PERIOD):
                        to_kill.append(sid)
                for sid in to_kill:
                    logger.info("Reaper: killing expired/dead session %s", sid[:8])
                    await self._kill_session_unlocked(sid)

    # ------------------------------------------------------------------
    # Session I/O
    # ------------------------------------------------------------------

    async def read_output(self, session_id: str) -> bytes | None:
        """Read the next chunk of PTY output.

        Returns ``bytes`` on data, ``b""`` on timeout (keep waiting),
        or ``None`` when the session has ended.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        try:
            item = await asyncio.wait_for(session._output_queue.get(), timeout=30)
            return item  # bytes or None (EOF sentinel)
        except asyncio.TimeoutError:
            return b""

    def write_input(self, session_id: str, data: bytes) -> None:
        """Write raw bytes to the PTY's stdin (master fd)."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            os.write(session.master_fd, data)
        except OSError:
            # PTY may have been closed by the reader thread or process exit
            logger.debug("PTY write failed for session %s", session_id, exc_info=True)

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        """Resize the PTY terminal dimensions."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        _set_winsize(session.master_fd, rows, cols)

    async def kill_session(self, session_id: str) -> None:
        """Terminate a PTY session and release all resources."""
        async with self._lock:
            await self._kill_session_unlocked(session_id)

    async def _kill_session_unlocked(self, session_id: str) -> None:
        """Terminate a session without acquiring ``self._lock``.

        Assumes the caller already holds ``self._lock``. Public callers
        should use ``kill_session`` instead; internal callers that already
        hold the lock (``_reaper_loop``, ``find_or_claim_session``) use
        this to avoid a non-reentrant deadlock.
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return

        # Close WebSocket if still connected (prevents stale handlers)
        if session._ws is not None:
            try:
                await session._ws.close(code=4001, reason="session killed")
            except Exception:
                logger.debug("Failed to close killed terminal websocket", exc_info=True)
            session._ws = None

        # Signal reader thread to stop
        session._stop_event.set()

        # Kill the entire process group
        _kill_process_group(session.process.pid)

        # Close master fd (unblocks reader thread if it's stuck in os.read)
        try:
            os.close(session.master_fd)
        except OSError:
            # fd may already be closed by the reader thread
            logger.debug("master_fd close failed for session %s", session_id, exc_info=True)

        # Wait for reader thread to finish
        if session._reader_thread and session._reader_thread.is_alive():
            session._reader_thread.join(timeout=2)

        # Push EOF sentinel so anyone waiting on read_output wakes up
        try:
            session._output_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

        # Clear output buffer
        session.output_buffer.clear()

        logger.info("Terminal session %s killed", session_id[:8])

    async def kill_all(self) -> None:
        """Kill every active PTY session (used at shutdown)."""
        # Stop the reaper first
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None

        ids = list(self._sessions.keys())
        for sid in ids:
            await self.kill_session(sid)
        if ids:
            logger.info("All terminal sessions killed (%d)", len(ids))


# ------------------------------------------------------------------
# Module-level helpers (pure functions / simple utilities)
# ------------------------------------------------------------------

def _safe_queue_put(queue: asyncio.Queue, item) -> None:
    """Queue put that silently drops on QueueFull.

    Used via ``loop.call_soon_threadsafe`` so that a full queue
    (no WS consumer during orphaned state) doesn't crash the reader.
    """
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass  # data is in OutputBuffer, safe to drop


def _reader_thread_func(
    fd: int,
    queue: asyncio.Queue,
    stop_event: threading.Event,
    loop: asyncio.AbstractEventLoop,
    tag: str,
    output_buffer: OutputBuffer,
) -> None:
    """Read from PTY master fd in a tight loop.

    Pushes data to both the ``OutputBuffer`` (always, for replay) and
    the ``asyncio.Queue`` (for live WS relay, drops on full).

    Runs in a dedicated ``threading.Thread``.
    """
    try:
        while not stop_event.is_set():
            try:
                data = os.read(fd, _READ_SIZE)
            except OSError:
                break  # fd closed
            if not data:
                break  # EOF
            # Always buffer for replay
            output_buffer.write(data)
            # Try to push to queue; if full (no WS consumer), data is still in buffer
            loop.call_soon_threadsafe(_safe_queue_put, queue, data)
    except Exception:
        logger.exception("Reader thread error for %s", tag)
    finally:
        # Signal EOF to the asyncio side
        loop.call_soon_threadsafe(_safe_queue_put, queue, None)
        logger.debug("Reader thread exiting for %s", tag)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set the terminal window size on *fd* via ``TIOCSWINSZ`` ioctl."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        # Non-fatal: resize lost; client can retry on next keystroke
        logger.debug("set_winsize ioctl failed", exc_info=True)


def _kill_process_group(pid: int) -> None:
    """Send SIGTERM then SIGKILL to the process group of *pid*."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            break
        if sig == signal.SIGTERM:
            time.sleep(0.15)


# Module-level singleton
terminal_service = TerminalService()
