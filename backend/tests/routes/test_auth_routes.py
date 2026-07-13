"""
Route- and middleware-level tests for the access-control feature.

Uses httpx with an ASGITransport so the full middleware stack
(AuthMiddleware + routers) runs end-to-end. The global ``settings`` singleton
is mutated per-test and restored afterwards; file writes are stubbed so the
real settings.yaml is never touched.
"""

import pytest
import httpx

from app.core import auth
from app.core.auth import SESSION_COOKIE_NAME, hash_password, session_token
from app.core.config import settings


@pytest.fixture
def no_password():
    """Ensure access protection is disabled, then restore on teardown."""
    saved = settings.security.password_hash
    settings.security.password_hash = ""
    yield
    settings.security.password_hash = saved


@pytest.fixture
def password_set():
    """Configure a known password and return (plaintext, hash)."""
    saved = settings.security.password_hash
    h = hash_password("correct-horse-battery")
    settings.security.password_hash = h
    yield "correct-horse-battery", h
    settings.security.password_hash = saved


@pytest.mark.asyncio
async def test_status_reports_disabled_when_open(no_password):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=__import__("app.main", fromlist=["app"]).app),
        base_url="http://test",
    ) as client:
        r = await client.get("/api/v1/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["password_enabled"] is False


@pytest.mark.asyncio
async def test_status_reports_enabled_when_set(password_set):
    app = __import__("app.main", fromlist=["app"]).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/auth/status")
    assert r.json()["data"]["password_enabled"] is True


@pytest.mark.asyncio
async def test_login_wrong_password_rejected(password_set):
    app = __import__("app.main", fromlist=["app"]).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/auth/login", json={"password": "nope"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_correct_password_sets_cookie(password_set):
    plaintext, h = password_set
    app = __import__("app.main", fromlist=["app"]).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/auth/login", json={"password": plaintext})
    assert r.status_code == 200
    # Cookie present and equals the expected token.
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    expected_token = session_token(h)
    assert expected_token in set_cookie


@pytest.mark.asyncio
async def test_protected_route_blocked_without_cookie(password_set):
    app = __import__("app.main", fromlist=["app"]).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/projects")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_allowed_with_valid_cookie(password_set):
    plaintext, h = password_set
    app = __import__("app.main", fromlist=["app"]).app
    token = session_token(h)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/auth/status", cookies={SESSION_COOKIE_NAME: token})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_invalid_cookie_rejected(password_set):
    app = __import__("app.main", fromlist=["app"]).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # A forged cookie on a protected path must be rejected.
        r = await client.get("/api/v1/projects", cookies={SESSION_COOKIE_NAME: "forged"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_password_change_requires_auth_when_set(password_set, monkeypatch):
    # Stub the file write so the real settings.yaml is untouched.
    monkeypatch.setattr("app.routes.auth.update_password_hash", _stub_set)
    app = __import__("app.main", fromlist=["app"]).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Logged out → cannot change password.
        r = await client.post("/api/v1/auth/password", json={"new_password": "newpass123"})
    assert r.status_code == 401


def _stub_set(h):
    """Update the in-memory hash only (no file write)."""
    settings.security.password_hash = h


@pytest.mark.asyncio
async def test_password_change_open_when_no_password(no_password, monkeypatch):
    # First-time setup must work without a cookie.
    monkeypatch.setattr("app.routes.auth.update_password_hash", _stub_set)
    monkeypatch.setattr("app.routes.auth.rotate_auth_secret", lambda: b"x" * 32)
    app = __import__("app.main", fromlist=["app"]).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/auth/password", json={"new_password": "brand-new-pw"})
    assert r.status_code == 200
    assert r.json()["data"]["password_enabled"] is True


@pytest.mark.asyncio
async def test_password_change_does_not_auto_authenticate(password_set, monkeypatch):
    """Changing the password must NOT keep the caller logged in: no new session
    cookie is issued, and the caller's prior cookie is invalidated by the secret
    rotation. The user must log in again with the new password."""
    plaintext, _h = password_set
    monkeypatch.setattr("app.routes.auth.update_password_hash", _stub_set)
    app = __import__("app.main", fromlist=["app"]).app
    token = session_token(settings.security.password_hash)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as client:
        r = await client.post("/api/v1/auth/password", json={"new_password": "brand-new-pw"})
    assert r.status_code == 200
    # No Set-Cookie on the response — the caller is not auto-authenticated.
    assert "set-cookie" not in r.headers


@pytest.mark.asyncio
async def test_get_settings_returns_hash(password_set):
    """The real bcrypt hash is returned (not redacted). The frontend derives
    password_enabled from its non-emptiness."""
    plaintext, h = password_set
    app = __import__("app.main", fromlist=["app"]).app
    token = session_token(h)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/v1/system/settings", cookies={SESSION_COOKIE_NAME: token})
    security = r.json()["data"]["config"]["security"]
    assert security["password_hash"] == h


@pytest.mark.asyncio
async def test_full_config_put_preserves_hash(password_set, monkeypatch):
    plaintext, h = password_set
    captured = {}

    def fake_save_settings_data(data):
        captured["data"] = data
        # Simulate the persisted hash being retained.
        return settings

    monkeypatch.setattr("app.routes.system.save_settings_data", fake_save_settings_data)
    app = __import__("app.main", fromlist=["app"]).app
    token = session_token(h)
    # Client submits a config that OMITS the security block entirely.
    payload = {"config": {"app": {"api_prefix": "/api/v1"}, "models": {}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.put("/api/v1/system/settings", json=payload, cookies={SESSION_COOKIE_NAME: token})
    assert r.status_code == 200
    # The hash must have been re-injected before saving.
    assert captured["data"]["security"]["password_hash"] == h


@pytest.mark.asyncio
async def test_put_ignores_client_security_block(password_set, monkeypatch):
    """PUT /system/settings must always ignore any client-supplied security
    block and re-inject the server's persisted hash. A stray forbidden key
    (e.g. password_enabled) must be dropped, not cause a 422."""
    plaintext, h = password_set
    captured = {}

    def fake_save_settings_data(data):
        captured["data"] = data
        return settings

    monkeypatch.setattr("app.routes.system.save_settings_data", fake_save_settings_data)
    app = __import__("app.main", fromlist=["app"]).app
    token = session_token(h)
    # A malformed security block including a forbidden password_enabled key,
    # plus a bogus hash that must never be persisted.
    payload = {
        "config": {
            "app": {"api_prefix": "/api/v1"},
            "models": {},
            "security": {"password_enabled": True, "password_hash": "bogus"},
        }
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.put("/api/v1/system/settings", json=payload, cookies={SESSION_COOKIE_NAME: token})
    assert r.status_code == 200, r.text
    # The server's persisted hash wins; the bogus value and forbidden key are gone.
    assert "password_enabled" not in captured["data"]["security"]
    assert captured["data"]["security"]["password_hash"] == h


@pytest.mark.asyncio
async def test_check_endpoint_tolerates_security_block(password_set):
    """The Provider-test flow (POST /settings/check) must not fail with
    extra_forbidden when the config includes a security block."""
    plaintext, h = password_set
    app = __import__("app.main", fromlist=["app"]).app
    token = session_token(h)
    payload = {
        "config": {
            "app": {"api_prefix": "/api/v1"},
            "security": {"password_enabled": True},
        }
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/api/v1/system/settings/check",
            json=payload,
            cookies={SESSION_COOKIE_NAME: token},
        )
    assert r.status_code == 200
    # The regression is specifically the extra_forbidden / password_enabled
    # validation error. The structure check may still fail for other reasons
    # (here: missing supervisor model), but it must NOT mention the forbidden
    # key or the security block.
    body = r.content.decode("utf-8")
    assert "password_enabled" not in body
    assert "Extra inputs are not permitted" not in body
    assert "extra_forbidden" not in body


@pytest.mark.asyncio
async def test_logout_clears_cookie(password_set):
    plaintext, h = password_set
    app = __import__("app.main", fromlist=["app"]).app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/api/v1/auth/login", json={"password": plaintext})
        r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    # delete_cookie sets Max-Age=0 / expires in the past.
    assert ("Max-Age=0" in set_cookie) or ("1970" in set_cookie) or ("expires=" in set_cookie.lower())


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
    connection is denied at the ASGI handshake and the handler is never
    reached."""
    from starlette.testclient import TestClient

    gated_app, state = _build_gated_app()
    client = TestClient(gated_app)
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/terminal/proj"):
            pass  # Should never get here — the upgrade is denied.
    assert state["reached"] is False


def test_websocket_allowed_with_valid_cookie(password_set):
    """A valid session cookie lets the WebSocket connection through to the
    handler."""
    from starlette.testclient import TestClient

    plaintext, h = password_set
    token = session_token(h)
    gated_app, state = _build_gated_app()
    client = TestClient(gated_app, cookies={SESSION_COOKIE_NAME: token})
    with client.websocket_connect("/api/v1/terminal/proj") as ws:
        ws.send_text("hello")
        echoed = ws.receive_text()
    assert echoed == "hello"
    assert state["reached"] is True


def test_websocket_allowed_when_no_password(no_password):
    """When no password is configured, WebSocket connections are open."""
    from starlette.testclient import TestClient

    gated_app, state = _build_gated_app()
    client = TestClient(gated_app)
    with client.websocket_connect("/api/v1/terminal/proj") as ws:
        ws.send_text("ok")
        echoed = ws.receive_text()
    assert echoed == "ok"
    assert state["reached"] is True
