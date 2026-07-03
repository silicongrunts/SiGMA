"""
Proxy route handlers for Jupyter (HTTP + WebSocket) and Browser VNC.

These functions are *not* decorated with @app.route here — they are
plain async callables that main.py registers on the FastAPI app instance.
"""

import asyncio

import httpx
import websockets
from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.logging import get_logger
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Jupyter proxy – WebSocket
# ---------------------------------------------------------------------------

async def jupyter_ws_proxy(websocket: WebSocket, path: str):
    """Proxy WebSocket connections to the embedded Jupyter server.

    Key settings to keep the kernel connection stable:
    - Disable the ``websockets`` library's built-in ping/pong so it does not
      interfere with Jupyter's own keep-alive mechanism.
    - Raise the max message size so large kernel outputs don't silently fail.
    """
    # Lazy import to avoid circular refs at module-load time
    from app.core.lifecycle import jupyter_service

    await websocket.accept()

    query_string = str(websocket.query_params)
    target_ws_url = f"ws://localhost:{jupyter_service.port}/api/v1/jupyter/{path}"
    if query_string:
        target_ws_url += f"?{query_string}"

    # Jupyter's JS drops the token after initial page-load and relies on
    # cookies instead.  Our proxy does NOT forward cookies to Jupyter, so we
    # must inject the token explicitly into every WebSocket URL.
    if "token=" not in target_ws_url:
        sep = "&" if "?" in target_ws_url else "?"
        target_ws_url += f"{sep}token={jupyter_service.token}"

    target_ws = None
    try:
        target_ws = await websockets.connect(
            target_ws_url,
            ping_interval=None,       # Let Jupyter handle keep-alive
            ping_timeout=None,
            max_size=2 ** 24,         # 16 MB – large kernel outputs
            close_timeout=5,
        )
        logger.info("WS proxy connected to Jupyter: %s", target_ws_url)

        async def _client_to_jupyter():
            """Forward messages from the browser client -> Jupyter."""
            try:
                while True:
                    try:
                        data = await websocket.receive()
                    except Exception:
                        logger.debug("Jupyter client websocket receive failed", exc_info=True)
                        break
                    msg_type = data.get("type", "")
                    if msg_type == "websocket.disconnect":
                        break
                    if "text" in data and data.get("text") is not None:
                        await target_ws.send(data["text"])
                    elif "bytes" in data and data.get("bytes") is not None:
                        await target_ws.send(data["bytes"])
            except websockets.ConnectionClosed as e:
                logger.debug("client->jupyter closed: %s", e)
            except Exception as e:
                logger.debug("client->jupyter error: %s", e, exc_info=True)

        async def _jupyter_to_client():
            """Forward messages from Jupyter -> browser client."""
            try:
                async for message in target_ws:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)
            except websockets.ConnectionClosed as e:
                logger.debug("jupyter->client closed: %s", e)
            except Exception as e:
                logger.debug("jupyter->client error: %s", e, exc_info=True)

        # Run both directions concurrently; stop when EITHER side closes.
        _, pending = await asyncio.wait(
            [
                asyncio.create_task(_client_to_jupyter()),
                asyncio.create_task(_jupyter_to_client()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Clean up remaining task
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as exc:
        logger.warning("Jupyter WS proxy error for %s: %s", path, exc, exc_info=True)
    finally:
        if target_ws:
            try:
                await target_ws.close()
            except Exception:
                logger.debug("Failed to close Jupyter target websocket", exc_info=True)
        try:
            await websocket.close()
        except Exception:
            logger.debug("Failed to close Jupyter client websocket", exc_info=True)


# ---------------------------------------------------------------------------
# Jupyter proxy – HTTP
# ---------------------------------------------------------------------------

async def jupyter_proxy(request: Request, path: str):
    """Reverse-proxy HTTP requests to the embedded Jupyter server."""
    from app.core.lifecycle import jupyter_service

    if not await jupyter_service.is_running():
        await jupyter_service.start()

    target_url = f"http://localhost:{jupyter_service.port}/api/v1/jupyter/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"

    req_headers = dict(request.headers)
    req_headers.pop("host", None)
    body = await request.body()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=req_headers,
                content=body,
                follow_redirects=False,
                timeout=60.0,
            )
    except Exception as exc:
        logger.warning("Jupyter HTTP proxy error for %s: %s", path, exc, exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"detail": f"Jupyter Proxy Error: {exc}"},
        )

    return StreamingResponse(
        resp.aiter_bytes(),
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


# ---------------------------------------------------------------------------
# Browser VNC WebSocket proxy
# ---------------------------------------------------------------------------

async def browser_vnc_ws_proxy(websocket: WebSocket, project_id: str):
    """Proxy WebSocket connections to the shared noVNC websockify server."""
    from app.services.browser_service import get_browser_service

    await websocket.accept()

    service = get_browser_service()

    # Auto-start the shared browser if not running
    status = await service.get_status()
    if status.get("status") != "running":
        logger.info("Shared browser not running, auto-starting for project %s", project_id)
        start_result = await service.start()
        if start_result.get("status") != "running":
            await websocket.close()
            return

    vnc_port = getattr(service, "PORT_WS", 6080)  # fall back to 6080 if PORT_WS is unset
    target_ws_url = f"ws://localhost:{vnc_port}"

    target_ws = None
    try:
        target_ws = await websockets.connect(
            target_ws_url,
            ping_interval=None,
            ping_timeout=None,
            max_size=2 ** 24,
            close_timeout=5,
        )
        logger.info("Browser VNC WS proxy connected for project %s", project_id)

        async def _client_to_vnc():
            try:
                while True:
                    try:
                        data = await websocket.receive()
                    except Exception:
                        logger.debug("Browser VNC client websocket receive failed", exc_info=True)
                        break
                    msg_type = data.get("type", "")
                    if msg_type == "websocket.disconnect":
                        break
                    if "bytes" in data and data.get("bytes") is not None:
                        await target_ws.send(data["bytes"])
                    elif "text" in data and data.get("text") is not None:
                        await target_ws.send(data["text"])
            except websockets.ConnectionClosed:
                pass
            except Exception:
                logger.debug("Browser VNC client-to-target proxy ended with error", exc_info=True)

        async def _vnc_to_client():
            try:
                async for message in target_ws:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
            except websockets.ConnectionClosed:
                pass
            except Exception:
                logger.debug("Browser VNC target-to-client proxy ended with error", exc_info=True)

        _, pending = await asyncio.wait(
            [asyncio.create_task(_client_to_vnc()), asyncio.create_task(_vnc_to_client())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as exc:
        logger.warning("Browser VNC WS proxy error for %s: %s", project_id, exc, exc_info=True)
    finally:
        if target_ws:
            try:
                await target_ws.close()
            except Exception:
                logger.debug("Failed to close browser VNC target websocket", exc_info=True)
        try:
            await websocket.close()
        except Exception:
            logger.debug("Failed to close browser VNC client websocket", exc_info=True)
