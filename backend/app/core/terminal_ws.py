"""
WebSocket endpoint for the integrated terminal.

Registered directly on the FastAPI app instance (same pattern as
``app.core.proxies``) because bidirectional relay does not fit the
thin-route-calls-service model.

Protocol
--------
Client → Server (JSON text frames):
    {"type": "input",  "data": "<base64-encoded bytes>"}
    {"type": "resize", "cols": <int>, "rows": <int>}
    {"type": "ping"}
    {"type": "kill"}                      — intentional close, kill session

Server → Client (binary frames):
    Raw PTY output bytes.
    On reconnection the full output buffer is sent first.

Keepalive
---------
When the WS disconnects the PTY session enters the ORPHANED state and
is kept alive for 10 minutes.  On reconnection (by matching
``project_id`` + ``slot``) the buffered output is replayed.

An intentional close (client sends ``{"type":"kill"}``) kills the
session immediately.  If a session is taken over by another WS
(generation mismatch) the stale handler silently exits.

Close codes:
    1000 — server-initiated normal close (relay ended or replay failed)
    3001 — client-initiated intentional kill: the frontend closes the socket
           with this code on explicit shutdown (distinct from the
           ``{"type":"kill"}`` JSON frame, which is a separate path)
    4001 — server-initiated takeover / kill (sent from ``terminal_service``
           when another handler claims the slot or the session is killed)
"""

import asyncio
import base64
import json
import time

from fastapi import WebSocket

from app.core.logging import get_logger
from app.services.terminal_service import SessionState, terminal_service

logger = get_logger(__name__)


async def terminal_ws_handler(websocket: WebSocket, project_id: str) -> None:
    """Handle a single terminal WebSocket connection."""
    await websocket.accept()

    # ── Read query parameters ──
    cols = _int_param(websocket, "cols", 80)
    rows = _int_param(websocket, "rows", 24)
    slot = _int_param(websocket, "slot", 1)

    # ── Acquire (or create) the session ──
    try:
        session, is_reattach = await terminal_service.find_or_claim_session(
            project_id, slot, cols, rows,
        )
    except FileNotFoundError:
        await _send_error(websocket, f"Project not found: {project_id}")
        return
    except RuntimeError as exc:
        await _send_error(websocket, str(exc))
        return
    except Exception as exc:
        logger.exception("Failed to acquire terminal session for %s slot %d", project_id, slot)
        await _send_error(websocket, str(exc))
        return

    session_id = session.session_id
    my_generation = session._ws_generation
    session._ws = websocket

    logger.info(
        "Terminal WS %s for project %s slot %d, session %s gen=%d",
        "reattached" if is_reattach else "connected",
        project_id, slot, session_id[:8], my_generation,
    )

    # ── Take replay snapshot BEFORE resize ──
    # The snapshot must happen before any SIGWINCH-triggered shell redraw,
    # because redraw escape sequences (\r\e[K) are designed to overwrite the
    # *current* cursor line in a live terminal.  In a replay they would
    # target the wrong line (the original prompt already ended with \r\n,
    # pushing the cursor down).  Let the redraw flow through the live
    # stream instead.
    if is_reattach:
        replay_data = session.output_buffer.snapshot()
        if replay_data:
            try:
                await websocket.send_bytes(replay_data)
            except Exception:
                await _cleanup(session_id, my_generation, intentional=False)
                try:
                    await websocket.close(code=1000)
                except Exception:
                    logger.debug("Failed to close terminal websocket after replay failure", exc_info=True)
                return
    else:
        # New session — PTY was just created with cols/rows from URL params.
        # This initial size set is fine; no shell redraw to worry about
        # because the session is brand new and the buffer is empty.
        terminal_service.resize(session_id, cols, rows)

    # ── Bidirectional relay ──

    async def _pty_to_ws() -> None:
        """Forward PTY output → WebSocket client."""
        try:
            while True:
                data = await terminal_service.read_output(session_id)
                if data is None:
                    break
                if data:
                    await websocket.send_bytes(data)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("pty→ws relay ended for %s: %s", session_id[:8], exc, exc_info=True)

    async def _ws_to_pty() -> None:
        """Forward WebSocket client → PTY input."""
        first_resize = True
        try:
            while True:
                try:
                    msg = await websocket.receive()
                except Exception:
                    logger.debug("Terminal websocket receive failed", exc_info=True)
                    break

                msg_type = msg.get("type", "")
                if msg_type == "websocket.disconnect":
                    if msg.get("code") == 3001:
                        await terminal_service.kill_session(session_id)
                    break

                text = msg.get("text")
                if text is None:
                    continue

                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue

                cmd = payload.get("type")
                if cmd == "input":
                    raw = base64.b64decode(payload.get("data", ""))
                    terminal_service.write_input(session_id, raw)
                elif cmd == "resize":
                    c = payload.get("cols")
                    r = payload.get("rows")
                    if isinstance(c, int) and isinstance(r, int):
                        terminal_service.resize(session_id, c, r)
                        if first_resize:
                            first_resize = False
                            # Pause buffering briefly so the shell's prompt
                            # redraw (\r\x1b[K ...) goes to the live stream
                            # but not the replay buffer.
                            session.output_buffer.pause_until = time.monotonic() + 0.3
                elif cmd == "kill":
                    session._ws = None
                    await terminal_service.kill_session(session_id)
                    break
                elif cmd == "ping":
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("ws→pty relay ended for %s: %s", session_id[:8], exc, exc_info=True)

    try:
        _, pending = await asyncio.wait(
            [asyncio.create_task(_pty_to_ws()), asyncio.create_task(_ws_to_pty())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    finally:
        await _cleanup(session_id, my_generation, intentional=False)
        try:
            await websocket.close(code=1000)
        except Exception:
            logger.debug("Failed to close terminal websocket", exc_info=True)
        logger.info("Terminal WS disconnected for session %s", session_id[:8])


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

async def _cleanup(session_id: str, my_generation: int, *, intentional: bool) -> None:
    """Detach or kill the session after WS disconnect.

    Only acts if *my_generation* still matches — a mismatch means the
    session was taken over by another handler.
    """
    session = terminal_service.get_session(session_id)
    if session is None:
        return
    if session._ws_generation != my_generation:
        logger.debug("Session %s gen mismatch on cleanup (mine=%d, now=%d) — skipping",
                      session_id[:8], my_generation, session._ws_generation)
        return

    session._ws = None

    if intentional or session.process.poll() is not None:
        await terminal_service.kill_session(session_id)
    elif session.state == SessionState.ACTIVE:
        await terminal_service.detach_session(session_id)


def _int_param(ws: WebSocket, name: str, default: int) -> int:
    try:
        return int(ws.query_params.get(name, str(default)))
    except (ValueError, TypeError):
        return default


async def _send_error(ws: WebSocket, message: str) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "message": message}))
    except Exception:
        logger.debug("Failed to send terminal websocket error", exc_info=True)
    try:
        await ws.close()
    except Exception:
        logger.debug("Failed to close terminal websocket after error", exc_info=True)
