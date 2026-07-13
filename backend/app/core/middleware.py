"""
Global middleware and exception handlers for SiGMA.

Registers exception handlers on the FastAPI app so all errors
return the unified API response format.
"""

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.auth import (
    is_authenticated_scope,
    is_password_change_path,
    is_public_path,
    password_enabled,
)
from app.core.exceptions import SiGMAException
from app.core.response import err
from app.core.logging import get_logger
from app.core.utils import generate_id


async def sigma_exception_handler(_request: Request, exc: SiGMAException) -> JSONResponse:
    """Handle all SiGMA custom exceptions."""
    logger = get_logger("sigma.errors")
    logger.warning(f"SiGMA error: [{exc.code}] {exc.message}")
    return err(exc.message, status_code=exc.status_code)


async def validation_exception_handler(_request: Request, exc) -> JSONResponse:
    """Handle Pydantic validation errors (FastAPI RequestValidationError)."""
    errors = []
    if hasattr(exc, "errors"):
        for e in exc.errors():
            errors.append({
                "field": ".".join(str(loc) for loc in e.get("loc", [])),
                "message": e.get("msg", ""),
            })
    return JSONResponse(
        status_code=422,
        content={
            "request_id": generate_id(),
            "success": False,
            "error": "Validation error",
            "data": {"errors": errors},
        },
    )


async def generic_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unhandled exceptions."""
    logger = get_logger("sigma.errors")
    logger.exception(f"Unhandled exception: {exc}")
    return err("Internal server error", status_code=500)


def register_exception_handlers(app) -> None:
    """Register all exception handlers on a FastAPI app."""
    from fastapi.exceptions import RequestValidationError

    app.add_exception_handler(SiGMAException, sigma_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)


# ---------------------------------------------------------------------------
# Access-control middleware
# ---------------------------------------------------------------------------

def _login_page_html() -> str:
    """Minimal self-contained login page served when an unauthenticated browser
    navigates to the SPA root.

    It posts to ``/api/v1/auth/login`` and reloads on success; the React app
    takes over once the cookie is set. Keeping this inline avoids any asset
    dependency for the locked-out state.
    """
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SiGMA — Sign in</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:#f6f7f9; color:#1f2937; display:flex; align-items:center;
         justify-content:center; min-height:100vh; }
  @media (prefers-color-scheme: dark) { body { background:#0f172a; color:#e5e7eb; } }
  .card { width:100%; max-width:380px; padding:40px 32px; background:#fff; border-radius:20px;
          box-shadow:0 10px 40px rgba(0,0,0,.12); }
  @media (prefers-color-scheme: dark) { .card { background:#1e293b; } }
  h1 { font-size:20px; font-weight:800; margin:0 0 4px; }
  p.sub { font-size:13px; color:#6b7280; margin:0 0 24px; }
  label { font-size:12px; font-weight:700; color:#6b7280; display:block; margin-bottom:6px; }
  input { width:100%; box-sizing:border-box; padding:11px 13px; font-size:14px;
          border:1px solid #e5e7eb; border-radius:10px; outline:none; background:transparent; color:inherit; }
  input:focus { border-color:#6d28d9; box-shadow:0 0 0 3px rgba(109,40,217,.12); }
  button { width:100%; margin-top:18px; padding:12px; font-size:14px; font-weight:700;
           color:#fff; background:#6d28d9; border:0; border-radius:10px; cursor:pointer; }
  button:hover { background:#5b21b6; }
  .err { color:#dc2626; font-size:13px; min-height:18px; margin-top:12px; }
</style>
</head>
<body>
  <form class="card" id="f">
    <h1>SiGMA</h1>
    <p class="sub">This instance is password protected. Sign in to continue.</p>
    <label for="pw">Password</label>
    <input id="pw" type="password" name="password" autocomplete="current-password" autofocus>
    <div class="err" id="e"></div>
    <button type="submit">Sign in</button>
  </form>
<script>
document.getElementById('f').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const e = document.getElementById('e'); e.textContent = '';
  const pw = document.getElementById('pw').value;
  try {
    const r = await fetch('/api/v1/auth/login', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ password: pw })
    });
    if (r.ok) { window.location.href = '/'; return; }
    e.textContent = 'Incorrect password';
  } catch (_) { e.textContent = 'Sign in failed. Please retry.'; }
});
</script>
</body>
</html>"""


class AuthMiddleware:
    """Enforce the shared-password access gate on every request.

    Implemented as a pure ASGI middleware (not ``BaseHTTPMiddleware``) so that
    WebSocket connections are covered too: ``BaseHTTPMiddleware.dispatch`` is
    only invoked for ``http`` scopes, so any ``websocket`` scope would bypass
    the gate entirely and reach the terminal / Jupyter / VNC proxies
    unauthenticated.

    - When no password is configured the middleware is a no-op — SiGMA stays
      fully open.
    - When a password is set, only paths in :func:`is_public_path` and the
      password-change endpoint (which self-guards) are reachable without a valid
      session cookie. Everything else — including the SPA catch-all, all
      ``/api/v1/*`` routers, and the WebSocket proxies — is blocked.
    - Blocked HTTP requests get a 401 JSON response (or the inline login page
      for browser navigations). Blocked WebSocket connections are denied at the
      ASGI handshake (``websocket.close`` code 4401) so they never reach the
      handler.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only HTTP and WebSocket are subject to the gate; ``lifespan`` and any
        # other scope types pass through untouched.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        if not password_enabled():
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # Public paths and the self-guarding password endpoint pass through.
        if is_public_path(path) or is_password_change_path(path):
            await self.app(scope, receive, send)
            return

        if is_authenticated_scope(scope):
            await self.app(scope, receive, send)
            return

        # Not authenticated — reject. The shape depends on the scope type.
        if scope["type"] == "websocket":
            await _reject_websocket(scope, send)
            return

        await _reject_http(scope, receive, send)


async def _reject_websocket(scope: Scope, send: Send) -> None:
    """Deny a WebSocket connection during the ASGI handshake.

    Sending ``websocket.close`` without a preceding ``websocket.accept`` tells
    the ASGI server to deny the upgrade, so the handler is never reached and no
    TCP WebSocket is established. Code 4401 is an application-level close code
    meaning "unauthorized".
    """
    await send({"type": "websocket.close", "code": 4401})


async def _reject_http(scope: Scope, receive: Receive, send: Send) -> None:
    """Send a 401 response for an unauthenticated HTTP request.

    API paths and non-safe methods get a JSON error; a browser navigation
    (GET/HEAD to a non-API path) gets the inline login page so the SPA root
    renders a sign-in form without needing any API call.
    """
    path = scope["path"]
    method = scope["method"]

    if path.startswith("/api/") or method in ("POST", "PUT", "PATCH", "DELETE"):
        response: Response = err("Authentication required", status_code=401)
    elif method in ("GET", "HEAD"):
        response = HTMLResponse(_login_page_html(), status_code=401)
    else:
        response = err("Authentication required", status_code=401)

    await response(scope, receive, send)
