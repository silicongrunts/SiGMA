"""
Stream Client — used by Huey Worker tasks to send streaming chunks
to the StreamServer running in the web process.

Lightweight reconnection logic: if the connection to the web process
drops (e.g. web restarted), the client retries up to 5 times with a
2-second backoff.  While disconnected chunks are silently dropped
(the worker continues executing and checkpoints to the project DB).
"""

import asyncio
import json
import struct
from typing import Optional

from app.core.logging import get_logger
from app.core.utils import generate_id

logger = get_logger(__name__)

HEADER = struct.Struct("!I")
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY = 2.0
PERMISSION_TIMEOUT = 300  # 5 minutes to wait for user response


class StreamClient:
    """Connects to StreamServer and sends streaming chunks.

    Also listens for server → worker commands (e.g. cancel) on the
    same TCP connection. The cancel_event is set when the server
    sends a cancel command, allowing the worker to abort mid-stream.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._connected = False
        self._task_id: Optional[str] = None
        self._project_id: Optional[str] = None
        self._reconnect_count = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.cancel_event: asyncio.Event = asyncio.Event()
        self._reader_task: Optional[asyncio.Task] = None
        self._pending_permissions: dict[str, dict] = {}  # request_id → {event, approved}

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------
    async def connect(self, task_id: str, project_id: str) -> bool:
        """Connect to StreamServer and send the hello handshake."""
        self._task_id = task_id
        self._project_id = project_id
        self._loop = asyncio.get_event_loop()
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port,
            )
            await self._send({
                "type": "hello",
                "task_id": task_id,
                "project_id": project_id,
            })
            self._connected = True
            self._reconnect_count = 0
            # Start background reader for server → worker commands
            self.cancel_event.clear()
            self._reader_task = asyncio.create_task(self._read_loop())
            logger.debug("Stream client connected for task %s", task_id)
            return True
        except Exception as exc:
            logger.warning("Stream client connect failed: %s", exc, exc_info=True)
            return False

    async def reconnect(self) -> bool:
        """Attempt to reconnect to StreamServer."""
        if self._reconnect_count >= MAX_RECONNECT_ATTEMPTS:
            logger.warning("Stream client gave up reconnecting after %d attempts", self._reconnect_count)
            return False
        self._reconnect_count += 1
        await asyncio.sleep(RECONNECT_DELAY * self._reconnect_count)
        logger.info("Stream client reconnecting (attempt %d/%d)", self._reconnect_count, MAX_RECONNECT_ATTEMPTS)
        # task_id and project_id were stored at connect time
        if self._task_id and self._project_id:
            return await self.connect(self._task_id, self._project_id)
        return False

    async def close(self) -> None:
        self._connected = False
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                logger.debug("Stream client close failed", exc_info=True)
            self._writer = None
            self._reader = None

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    async def _send(self, msg: dict) -> None:
        if not self._writer:
            raise ConnectionError("Not connected")
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        self._writer.write(HEADER.pack(len(body)) + body)
        await self._writer.drain()

    async def _safe_send(self, msg: dict) -> bool:
        """Send with auto-reconnect on failure. Returns True if sent."""
        try:
            await self._send(msg)
            return True
        except OSError as exc:
            logger.warning("Stream send failed: %s", exc)
            await self.close()
            if await self.reconnect():
                try:
                    await self._send(msg)
                    return True
                except Exception:
                    logger.debug("Stream send retry failed", exc_info=True)
            return False

    async def send_chunk(self, sse_event: str) -> None:
        """Send a streaming chunk (best-effort — silently skips if disconnected)."""
        if not self._connected:
            return
        await self._safe_send({"type": "chunk", "task_id": self._task_id, "data": sse_event})

    async def send_heartbeat(self) -> None:
        if not self._connected:
            return
        await self._safe_send({"type": "heartbeat", "task_id": self._task_id})

    async def send_done(self) -> None:
        if self._connected:
            await self._safe_send({"type": "done", "task_id": self._task_id})
        await self.close()

    async def send_error(self, message: str, data: dict | None = None) -> None:
        if self._connected:
            payload = {"type": "error", "task_id": self._task_id, "message": message}
            if data:
                payload["data"] = data
            await self._safe_send(payload)
        await self.close()

    # ------------------------------------------------------------------
    # Permission requests (worker → server → frontend → user → server → worker)
    # ------------------------------------------------------------------
    async def request_permission(self, tool: str, path: str, operation: str,
                                content: str = "", description: str = "") -> dict:
        """Send a permission request to the frontend and wait for user response.

        Returns ``{"approved": bool, "reason": str}``. ``description`` is an
        optional intent label shown in the approval dialog.
        """
        request_id = generate_id()
        event = asyncio.Event()
        self._pending_permissions[request_id] = {"event": event, "approved": False}

        payload = {
            "type": "permission_request",
            "task_id": self._task_id,
            "request_id": request_id,
            "tool": tool,
            "path": path,
            "operation": operation,
        }
        if content:
            payload["content"] = content
        if description:
            payload["description"] = description

        sent = await self._safe_send(payload)

        if not sent:
            self._pending_permissions.pop(request_id, None)
            return {"approved": False, "reason": ""}

        try:
            await asyncio.wait_for(event.wait(), timeout=PERMISSION_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending_permissions.pop(request_id, None)
            logger.warning("Permission request %s timed out", request_id)
            return {"approved": False, "reason": ""}

        result = self._pending_permissions.pop(request_id, None)
        if result:
            return {"approved": result.get("approved", False), "reason": result.get("reason", "")}
        return {"approved": False, "reason": ""}

    # ------------------------------------------------------------------
    # Receiving (server → worker commands)
    # ------------------------------------------------------------------
    async def _read_loop(self) -> None:
        """Background task: read commands from StreamServer."""
        try:
            while self._reader and not self._reader.at_eof():
                msg = await self._read_one()
                if msg is None:
                    break
                msg_type = msg.get("type")

                if msg_type == "cancel":
                    logger.info("Received cancel signal from server")
                    self.cancel_event.set()

                elif msg_type == "permission_response":
                    request_id = msg.get("request_id", "")
                    pending = self._pending_permissions.get(request_id)
                    if pending:
                        pending["approved"] = msg.get("approved", False)
                        pending["reason"] = msg.get("reason", "")
                        pending["event"].set()
                    else:
                        logger.warning("Received permission_response for unknown request %s", request_id)

        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass  # normal disconnect / shutdown
        except Exception as exc:
            logger.debug("Stream client reader stopped: %s", exc, exc_info=True)

    async def _read_one(self) -> Optional[dict]:
        """Read a single length-prefixed JSON message."""
        try:
            hdr = await self._reader.readexactly(HEADER.size)
        except asyncio.IncompleteReadError:
            return None
        length = HEADER.unpack(hdr)[0]
        payload = await self._reader.readexactly(length)
        return json.loads(payload.decode("utf-8"))
