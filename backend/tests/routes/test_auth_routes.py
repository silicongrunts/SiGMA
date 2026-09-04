"""
Route- and middleware-level tests for the access-control feature.

Uses httpx with an ASGITransport (the shared ``client`` fixture in this
directory's conftest) so the full middleware stack (AuthMiddleware + routers)
runs end-to-end. The global ``settings`` singleton is mutated per-test and
restored afterwards; the settings.yaml write and the signing-secret file are
stubbed/redirected (conftest's autouse ``isolate_auth_secret``), so the real
userdata files are never touched.
"""

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.auth import SESSION_COOKIE_NAME, session_token
from app.core.config import settings
from app.routes import projects

TEST_PASSWORD = "correct-horse-battery"


def _stub_set(password_hash):
    """Update the in-memory hash only (no settings.yaml write)."""
    settings.security.password_hash = password_hash


@pytest.mark.asyncio
async def test_status_reports_disabled_when_open(no_password, client):
    r = await client.get("/api/v1/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["password_enabled"] is False


@pytest.mark.asyncio
async def test_status_reports_enabled_when_set(password_set, client):
    r = await client.get("/api/v1/auth/status")
    assert r.json()["data"]["password_enabled"] is True


@pytest.mark.asyncio
async def test_login_wrong_password_rejected(password_set, client):
    r = await client.post("/api/v1/auth/login", json={"password": "nope"})
    assert r.status_code == 401
    # Deliberately generic (see routes/auth.py): the body must be the plain
    # AUTHENTICATION_ERROR envelope and reveal nothing about the configured
    # password or its hash.
    body = r.json()
    assert set(body) == {"request_id", "success", "error", "data"}
    assert body["success"] is False
    assert body["error"] == "Incorrect password"
    assert body["data"] is None
    assert "password_hash" not in r.text
    assert "password_enabled" not in r.text


@pytest.mark.asyncio
async def test_login_correct_password_sets_cookie(password_set, client):
    r = await client.post("/api/v1/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200
    # Cookie present and equals the expected token.
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert session_token(password_set) in set_cookie


@pytest.mark.asyncio
async def test_protected_route_blocked_without_cookie(password_set, client):
    r = await client.get("/api/v1/projects")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_allowed_with_valid_cookie(password_set, client, monkeypatch):
    # A protected route (not on the public allow-list) must accept a valid
    # session cookie; the project listing itself is stubbed out.
    async def list_projects():
        return []

    monkeypatch.setattr(
        projects, "project_service", SimpleNamespace(list_projects=list_projects),
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_token(password_set))
    r = await client.get("/api/v1/projects")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_invalid_cookie_rejected(password_set, client):
    # A forged cookie on a protected path must be rejected.
    client.cookies.set(SESSION_COOKIE_NAME, "forged")
    r = await client.get("/api/v1/projects")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_password_change_requires_auth_when_set(password_set, client):
    # Logged out → cannot change password (the route self-guards before any
    # password write, so no stubbing is needed here).
    r = await client.post("/api/v1/auth/password", json={"new_password": "newpass123"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_password_change_open_when_no_password(no_password, client, monkeypatch):
    # First-time setup must work without a cookie. The signing-secret rotation
    # runs for real against the conftest's temporary secret path.
    monkeypatch.setattr("app.routes.auth.update_password_hash", _stub_set)
    r = await client.post("/api/v1/auth/password", json={"new_password": "brand-new-pw"})
    assert r.status_code == 200
    assert r.json()["data"]["password_enabled"] is True


@pytest.mark.asyncio
async def test_password_change_does_not_auto_authenticate(password_set, client, monkeypatch):
    """Changing the password must NOT keep the caller logged in: no new session
    cookie is issued, and the caller's prior cookie is invalidated by the secret
    rotation. The user must log in again with the new password."""
    monkeypatch.setattr("app.routes.auth.update_password_hash", _stub_set)
    client.cookies.set(SESSION_COOKIE_NAME, session_token(password_set))
    r = await client.post("/api/v1/auth/password", json={"new_password": "brand-new-pw"})
    assert r.status_code == 200
    # No Set-Cookie on the response — the caller is not auto-authenticated.
    assert "set-cookie" not in r.headers


@pytest.mark.asyncio
async def test_logout_clears_cookie(password_set, client):
    await client.post("/api/v1/auth/login", json={"password": TEST_PASSWORD})
    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    # delete_cookie must actually expire the cookie: Max-Age=0 and an expiry
    # timestamp in the past.
    assert "Max-Age=0" in set_cookie
    expires = re.search(r"expires=([^;]+)", set_cookie, re.IGNORECASE).group(1)
    assert parsedate_to_datetime(expires) < datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# WebSocket authentication — the central security regression.
#
# BaseHTTPMiddleware.dispatch is never called for websocket scopes, so a
# request-level gate would let WebSockets through. AuthMiddleware is a pure
# ASGI middleware precisely to close that hole. These tests build a minimal
# app wrapping AuthMiddleware around an echo handler and assert that the gate
# covers WebSocket connections.
# ---------------------------------------------------------------------------

def _build_gated_app():
    """A minimal ASGI app: AuthMiddleware wrapping an echo WebSocket handler.

    Returns the gated app and a flag the inner handler sets when reached, so a
    test can tell whether the middleware let the connection through.
    """
    from app.core.middleware import AuthMiddleware

    state = {"reached": False}

    async def inner(scope, receive, send):
        if scope["type"] == "websocket":
            state["reached"] = True
            await send({"type": "websocket.accept"})
            # The first receive is the connection handshake message; the actual
            # client payload arrives on the second receive.
            await receive()
            msg = await receive()
            await send({"type": "websocket.send", "text": msg.get("text", "")})
            await send({"type": "websocket.close"})
        # HTTP scopes are handled by the real app in other tests; here we only
        # care about WebSocket coverage.

    return AuthMiddleware(inner), state


def test_websocket_blocked_without_cookie(password_set):
    """No cookie on a WebSocket connection when a password is set → the
    connection is denied at the ASGI handshake (WebSocketDisconnect) and the
    handler is never reached."""
    gated_app, state = _build_gated_app()
    client = TestClient(gated_app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/terminal/proj"):
            pass  # Should never get here — the upgrade is denied.
    assert state["reached"] is False


def test_websocket_allowed_with_valid_cookie(password_set):
    """A valid session cookie lets the WebSocket connection through to the
    handler."""
    token = session_token(password_set)
    gated_app, state = _build_gated_app()
    client = TestClient(gated_app, cookies={SESSION_COOKIE_NAME: token})
    with client.websocket_connect("/api/v1/terminal/proj") as ws:
        ws.send_text("hello")
        echoed = ws.receive_text()
    assert echoed == "hello"
    assert state["reached"] is True


def test_websocket_allowed_when_no_password(no_password):
    """When no password is configured, WebSocket connections are open."""
    gated_app, state = _build_gated_app()
    client = TestClient(gated_app)
    with client.websocket_connect("/api/v1/terminal/proj") as ws:
        ws.send_text("ok")
        echoed = ws.receive_text()
    assert echoed == "ok"
    assert state["reached"] is True
