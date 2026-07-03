"""
Stream Server — lightweight TCP server that relays streaming chunks from
Huey Workers to waiting SSE connections in the web process.

Protocol (JSON-over-TCP, length-prefixed):
  Worker → Server:
    {"type": "hello",   "task_id": "...", "project_id": "..."}
    {"type": "chunk",   "task_id": "...", "data": "event: delta\\ndata: {...}\\n\\n"}
    {"type": "heartbeat", "task_id": "..."}
    {"type": "done",    "task_id": "..."}
    {"type": "error",   "task_id": "...", "message": "..."}
    {"type": "permission_request", "task_id": "...", "request_id": "...",
     "tool": "write", "path": "/path/to/file", "operation": "write"}

  Server → Worker:
    {"type": "cancel"}
    {"type": "permission_response", "request_id": "...", "approved": true/false}
"""

import asyncio
import json
import struct
from typing import Dict, Optional

from app.core.logging import get_logger
from app.database.unit_of_work import UnitOfWork

logger = get_logger(__name__)

HEADER = struct.Struct("!I")                     # 4-byte big-endian length prefix
MAX_MSG = 10 * 1024 * 1024                       # 10 MB sanity cap
SESSION_RETENTION_SECONDS = 300                  # keep session for late SSE reconnects
STREAM_LIVENESS_POLL_SECONDS = 5
STREAM_STALE_GRACE_SECONDS = 120


def _worker_error_payload(msg: dict) -> tuple[str, dict]:
    """Return the persisted task error message and frontend SSE payload."""
    message = msg.get("message", "Unknown error")
    payload = msg.get("data")
    if not isinstance(payload, dict):
        return message, {"error": message}
    if "error" not in payload:
        payload = {**payload, "error": message}
    return message, payload


# ---------------------------------------------------------------------------
# Stream session — one per active task
# ---------------------------------------------------------------------------
class StreamSession:
    def __init__(self, task_id: str, project_id: str):
        self.task_id = task_id
        self.project_id = project_id
        self.buffer: list = []                    # recent chunks for reconnect catch-up
        self.subscribers: list = []               # asyncio.Queue per SSE connection
        self.done = False
        self.error: Optional[str] = None
        self.chunk_count = 0
        self._writer: Optional[asyncio.StreamWriter] = None  # worker TCP socket

    def push(self, data: str) -> None:
        self.buffer.append(data)
        # keep buffer bounded — last 200 chunks for catch-up
        if len(self.buffer) > 200:
            self.buffer = self.buffer[-200:]
        self.chunk_count += 1
        for q in self.subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def subscribe(self, q: asyncio.Queue) -> None:
        """Attach an SSE subscriber and replay buffered chunks."""
        self.subscribers.append(q)
        for chunk in self.buffer[-200:]:          # send recent chunks for catch-up
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                break

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self.subscribers.remove(q)
        except ValueError:
            pass

    async def shutdown(self) -> None:
        self.done = True
        for q in self.subscribers:
            try:
                q.put_nowait("event: done\ndata: {}\n\n")
            except asyncio.QueueFull:
                pass

    async def send_cancel(self) -> None:
        """Send a cancel command to the worker over its TCP connection."""
        if self._writer and not self._writer.is_closing():
            try:
                await _write_one(self._writer, {"type": "cancel"})
                logger.info("Sent cancel to worker for task %s", self.task_id)
            except Exception as exc:
                logger.warning("Failed to send cancel to worker: %s", exc, exc_info=True)

    async def send_permission_response(self, request_id: str, approved: bool, reason: str = "") -> None:
        """Send a permission response to the worker over its TCP connection."""
        if self._writer and not self._writer.is_closing():
            try:
                msg = {
                    "type": "permission_response",
                    "request_id": request_id,
                    "approved": approved,
                }
                if reason:
                    msg["reason"] = reason
                await _write_one(self._writer, msg)
                logger.info("Sent permission response (%s) for request %s",
                            "approved" if approved else "denied", request_id)
            except Exception as exc:
                logger.warning("Failed to send permission response: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Stream Server
# ---------------------------------------------------------------------------
class StreamServer:
    """TCP server that accepts connections from Huey Workers and relays
    streaming chunks to SSE subscribers."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self._server: Optional[asyncio.Server] = None
        self.sessions: Dict[str, StreamSession] = {}

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port,
        )
        logger.info("Stream server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Shut down all sessions
        for task_id in list(self.sessions):
            await self.sessions[task_id].shutdown()
        self.sessions.clear()
        logger.info("Stream server stopped")

    # -- session access ----------------------------------------------------
    def get(self, task_id: str) -> Optional[StreamSession]:
        return self.sessions.get(task_id)

    # -- cancel a running task ----------------------------------------------
    async def cancel_task(self, task_id: str) -> bool:
        """Send a cancel signal to the worker for the given task.

        Returns True if the signal was sent, False if the session was not
        found (worker may have already finished).
        """
        session = self.sessions.get(task_id)
        if not session:
            return False
        await session.send_cancel()
        return True

    async def cancel_project(self, project_id: str) -> int:
        """Cancel and remove all stream sessions for a deleted project."""
        task_ids = [
            task_id for task_id, session in self.sessions.items()
            if session.project_id == project_id
        ]
        for task_id in task_ids:
            session = self.sessions.pop(task_id, None)
            if not session:
                continue
            await session.send_cancel()
            await session.shutdown()
        return len(task_ids)

    # -- permission response (web → worker) ---------------------------------
    async def respond_permission(self, task_id: str, request_id: str, approved: bool, reason: str = "") -> bool:
        """Send a permission approval/denial to the worker.

        Returns True if the signal was sent, False if the session was not
        found.
        """
        session = self.sessions.get(task_id)
        if not session:
            return False
        await session.send_permission_response(request_id, approved, reason)
        return True

    # -- SSE subscriber -----------------------------------------------------
    async def subscribe(self, task_id: str, timeout: int = 1800):
        """Subscribe to a task's SSE stream. Yields SSE-formatted strings.

        Blocks until the worker connects (creates the session) and pushes chunks.
        Yields each chunk as it arrives. Yields a final 'done' event when the
        worker signals completion, or an 'error' event on failure.
        """
        # Wait for the worker to connect and create the session
        waited = 0.0
        while task_id not in self.sessions and waited < 30:  # 30s connect timeout
            await asyncio.sleep(0.1)
            waited += 0.1

        session = self.sessions.get(task_id)
        if session is None:
            yield 'event: error\ndata: {"error": "Worker did not connect"}\n\n'
            return

        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        session.subscribe(q)

        idle = 0
        stale_notice_sent = False
        stale_since: float | None = None
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        q.get(), timeout=STREAM_LIVENESS_POLL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    idle += STREAM_LIVENESS_POLL_SECONDS
                    if session.project_id:
                        async with UnitOfWork(session.project_id) as uow:
                            liveness = await uow.task_state.check_liveness(task_id)
                        if liveness == "stale":
                            now = asyncio.get_running_loop().time()
                            if stale_since is None:
                                stale_since = now
                            if not stale_notice_sent:
                                stale_notice_sent = True
                                yield (
                                    "event: stream_status\ndata: "
                                    + json.dumps(
                                        {
                                            "status": "waiting",
                                            "message": (
                                                "Stream is waiting for the worker. "
                                                "The task may still be running."
                                            ),
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                )
                            if now - stale_since >= STREAM_STALE_GRACE_SECONDS:
                                message = (
                                    "Worker stopped sending heartbeats and did not recover. "
                                    "The task was marked failed."
                                )
                                async with UnitOfWork(session.project_id) as uow:
                                    await uow.task_state.mark_failed(task_id, message)
                                session.done = True
                                session.error = message
                                yield (
                                    "event: error\ndata: "
                                    + json.dumps(
                                        {"error": message}, ensure_ascii=False,
                                    )
                                    + "\n\n"
                                )
                                asyncio.get_running_loop().call_later(
                                    SESSION_RETENTION_SECONDS, lambda: self.sessions.pop(task_id, None),
                                )
                                return
                            continue
                        stale_since = None
                        stale_notice_sent = False
                        if liveness in ("completed", "failed"):
                            if liveness == "failed":
                                session.done = True
                                yield (
                                    "event: error\ndata: "
                                    + json.dumps(
                                        {"error": "Task failed"}, ensure_ascii=False,
                                    )
                                    + "\n\n"
                                )
                            else:
                                session.done = True
                                yield "event: done\ndata: {}\n\n"
                            asyncio.get_running_loop().call_later(
                                SESSION_RETENTION_SECONDS, lambda: self.sessions.pop(task_id, None),
                            )
                            return
                    if idle >= timeout:
                        yield (
                            'event: error\ndata: {"error": "Stream connection lost"}\n\n'
                        )
                        return
                    continue
                idle = 0

                yield data

                # If this is a done or error event, stop
                if data.startswith("event: done") or data.startswith("event: error"):
                    return
        finally:
            session.unsubscribe(q)

    # -- connection handler ------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task_id = None
        session = None
        try:
            # First message must be hello
            msg = await _read_one(reader)
            if not msg or msg.get("type") != "hello":
                logger.warning("Stream connection rejected: no hello message")
                writer.close()
                return

            task_id = msg["task_id"]
            project_id = msg.get("project_id", "")

            # Create or reuse session
            session = self.sessions.get(task_id)
            if not session:
                session = StreamSession(task_id, project_id)
                self.sessions[task_id] = session
            else:
                # Reset session for reuse by a new task (e.g. reprocess after cancel)
                session.done = False
                session.error = None
                session.buffer.clear()
                session.chunk_count = 0
            session._writer = writer  # store for server → worker commands

            logger.debug("Stream worker connected for task %s", task_id)

            # Read remaining messages
            while True:
                msg = await _read_one(reader)
                if msg is None:
                    break
                msg_type = msg.get("type", "")

                if msg_type == "chunk":
                    session.push(msg.get("data", ""))

                elif msg_type == "done":
                    session.push("event: done\ndata: {}\n\n")
                    session.done = True

                    # Don't overwrite awaiting_input (interactive tool paused)
                    if project_id:
                        async with UnitOfWork(project_id) as uow:
                            liveness = await uow.task_state.check_liveness(task_id)
                            if liveness != "awaiting_input":
                                await uow.task_state.mark_completed(task_id)

                    logger.info("Task %s completed (%d chunks)", task_id, session.chunk_count)
                    # Keep session alive for 5 minutes for late SSE reconnects
                    asyncio.get_running_loop().call_later(
                        SESSION_RETENTION_SECONDS, lambda: self.sessions.pop(task_id, None),
                    )
                    break

                elif msg_type == "error":
                    session.error, error_payload = _worker_error_payload(msg)
                    session.push(
                        "event: error\ndata: "
                        + json.dumps(error_payload, ensure_ascii=False)
                        + "\n\n"
                    )
                    session.done = True

                    if project_id:
                        async with UnitOfWork(project_id) as uow:
                            await uow.task_state.mark_failed(task_id, session.error)

                    asyncio.get_running_loop().call_later(
                        SESSION_RETENTION_SECONDS, lambda: self.sessions.pop(task_id, None),
                    )
                    break

                elif msg_type == "heartbeat":
                    if project_id:
                        async with UnitOfWork(project_id) as uow:
                            await uow.task_state.heartbeat(task_id)

                elif msg_type == "permission_request":
                    # Forward to SSE clients (frontend) as a permission_request event
                    request_id = msg.get("request_id", "")
                    perm_payload = {
                        "task_id": task_id,
                        "request_id": request_id,
                        "tool": msg.get("tool", ""),
                        "path": msg.get("path", ""),
                        "operation": msg.get("operation", "write"),
                    }
                    if msg.get("content"):
                        perm_payload["content"] = msg["content"]
                    if msg.get("description"):
                        perm_payload["description"] = msg["description"]
                    session.push(
                        "event: permission_request\ndata: "
                        + json.dumps(perm_payload, ensure_ascii=False)
                        + "\n\n"
                    )
                    logger.info("Permission request %s from task %s: %s %s",
                                request_id, task_id, msg.get("tool"), msg.get("path"))

        except asyncio.IncompleteReadError:
            pass  # handled in finally if the task did not send done/error
        except Exception as exc:
            logger.error("Stream connection error (task %s): %s", task_id, exc, exc_info=True)
        finally:
            if session and not session.done:
                message = (
                    "Worker stream disconnected. The task may still be running; "
                    "waiting for the saved task state."
                )
                session.push(
                    "event: stream_status\ndata: "
                    + json.dumps(
                        {"status": "waiting", "message": message},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                session._writer = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                logger.debug("Stream worker close failed for task %s", task_id, exc_info=True)
            logger.debug("Stream worker disconnected for task %s", task_id)


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------
async def _read_one(reader: asyncio.StreamReader) -> Optional[dict]:
    try:
        header = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError:
        return None
    length = HEADER.unpack(header)[0]
    if length > MAX_MSG:
        raise ValueError(f"Message too large: {length}")
    payload = await reader.readexactly(length)
    return json.loads(payload.decode("utf-8"))


def _encode(msg: dict) -> bytes:
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    return HEADER.pack(len(body)) + body


async def _write_one(writer: asyncio.StreamWriter, msg: dict) -> None:
    """Write a single length-prefixed JSON message to a writer."""
    writer.write(_encode(msg))
    await writer.drain()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
stream_server = StreamServer()
