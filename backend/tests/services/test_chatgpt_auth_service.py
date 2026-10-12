"""Tests for the ChatGPT subscription auth service.

Covers the canonical/mirror token files, the proactive refresh chain
(including terminal refresh failures), CLI auth-file import (which also
supersedes a login in flight), the paste-path login flow end-to-end (with
the embedded-Chrome leg stubbed out), manual own-browser login, the status
details exposed while a login is pending, and the loopback callback
listener against real HTTP.
"""

import asyncio
import base64
import json
import time

import httpx
import pytest

import app.services.chatgpt_auth_service as auth_module
from app.core.exceptions import ChatGPTAuthError, ConfigurationError
from app.services.chatgpt_auth_service import ChatGPTAuthService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(claims: dict) -> str:
    return f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.signature"


def _claims(email="user@example.com", plan="plus", exp=None):
    auth = {"chatgpt_account_id": "acct-123", "chatgpt_plan_type": plan}
    return {
        "email": email,
        "chatgpt_plan_type": plan,
        "chatgpt_account_id": "acct-123",
        "https://api.openai.com/auth": auth,
        "exp": exp if exp is not None else int(time.time()) + 86400,
    }


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeClient:
    """httpx.AsyncClient stand-in driven by a per-test handler."""

    handler = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, data=None, headers=None):
        return type(self).handler("POST", url, data=data)

    async def get(self, url, headers=None):
        return type(self).handler("GET", url)

    async def request(self, method, url, headers=None, json=None):
        return type(self).handler(method, url, headers=headers, json_body=json)


@pytest.fixture
def service(tmp_path, monkeypatch) -> ChatGPTAuthService:
    """A fresh service whose token files live in a temp userdata tree."""
    canonical = tmp_path / "chatgpt.auth.json"
    token_dir = tmp_path / ".SiGMA" / "chatgpt"
    monkeypatch.setattr(auth_module, "CHATGPT_AUTH_FILE", canonical)
    monkeypatch.setattr(auth_module, "CHATGPT_TOKEN_DIR", token_dir)
    svc = ChatGPTAuthService()
    yield svc


@pytest.fixture
def fake_http(monkeypatch):
    def install(handler):
        _FakeClient.handler = handler
        monkeypatch.setattr(auth_module.httpx, "AsyncClient", _FakeClient)
    return install


def _write_record(svc: ChatGPTAuthService, **overrides):
    record = {
        "access_token": _jwt(_claims()),
        "refresh_token": "rt-old",
        "id_token": _jwt(_claims()),
        "expires_at": int(time.time()) + 5 * 86400,
        "account_id": "acct-123",
        "email": "user@example.com",
        "plan_type": "plus",
        "updated_at": int(time.time()),
    }
    record.update(overrides)
    svc._persist(record)
    return record


# ---------------------------------------------------------------------------
# Records & files
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_persist_writes_canonical_and_locked_mirror(service):
    record = _write_record(service)

    canonical = json.loads(auth_module.CHATGPT_AUTH_FILE.read_text())
    assert canonical["email"] == "user@example.com"
    assert canonical["expires_at"] == record["expires_at"]

    mirror_path = auth_module.CHATGPT_TOKEN_DIR / "auth.json"
    mirror = json.loads(mirror_path.read_text())
    assert set(mirror) == {
        "access_token", "refresh_token", "id_token", "expires_at", "account_id",
    }
    assert mirror["access_token"] == record["access_token"]

    import os
    assert os.stat(auth_module.CHATGPT_AUTH_FILE).st_mode & 0o777 == 0o600
    assert os.stat(mirror_path).st_mode & 0o777 == 0o600
    assert os.stat(auth_module.CHATGPT_TOKEN_DIR).st_mode & 0o777 == 0o700


@pytest.mark.unit
def test_build_record_extracts_jwt_metadata(service):
    record = service._build_record({
        "access_token": _jwt(_claims(exp=int(time.time()) + 9999)),
        "refresh_token": "rt",
        "id_token": _jwt(_claims(email="jane@example.com", plan="pro")),
        "expires_in": 1000,
    })
    assert record["email"] == "jane@example.com"
    assert record["plan_type"] == "pro"
    assert record["account_id"] == "acct-123"
    assert record["expires_at"] <= time.time() + 1000


# ---------------------------------------------------------------------------
# Refresh chain
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_refresh_within_margin_rotates_tokens(service, fake_http):
    old_record = _write_record(service, expires_at=int(time.time()) + 3600)  # inside 24h margin
    new_token = _jwt(_claims(email="refreshed@example.com"))

    def handler(method, url, data=None):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "rt-old"
        assert data["scope"] == "openid profile email"
        return _FakeResponse(payload={
            "access_token": new_token,
            "refresh_token": "rt-new",
            "id_token": _jwt(_claims()),
            "expires_in": 10 * 86400,
        })

    fake_http(handler)
    token = await service.get_access_token()
    # The refreshed token — not the pre-refresh one — must reach the caller.
    assert token == new_token
    assert token != old_record["access_token"]
    stored = json.loads(auth_module.CHATGPT_AUTH_FILE.read_text())
    assert stored["refresh_token"] == "rt-new"
    assert stored["expires_at"] > time.time() + 9 * 86400


@pytest.mark.unit
async def test_refresh_keeps_old_token_when_rotation_omitted(service, fake_http):
    _write_record(service, expires_at=int(time.time()) + 3600)

    def handler(method, url, data=None):
        return _FakeResponse(payload={
            "access_token": _jwt(_claims()),
            "expires_in": 10 * 86400,           # no refresh_token in response
        })

    fake_http(handler)
    await service.get_access_token()
    stored = json.loads(auth_module.CHATGPT_AUTH_FILE.read_text())
    assert stored["refresh_token"] == "rt-old"


@pytest.mark.unit
async def test_no_refresh_outside_margin(service, fake_http):
    _write_record(service)  # 5 days remaining

    def handler(method, url, data=None):
        raise AssertionError("refresh must not run outside the margin")

    fake_http(handler)
    token = await service.get_access_token()
    assert token


@pytest.mark.unit
async def test_terminal_refresh_error_requires_relogin(service, fake_http):
    _write_record(service, expires_at=int(time.time()) + 3600)
    calls = []

    def handler(method, url, data=None):
        calls.append(1)
        return _FakeResponse(status_code=400, payload={"error": "invalid_grant"})

    fake_http(handler)
    with pytest.raises(ChatGPTAuthError):
        await service.get_access_token()
    assert len(calls) == 1                  # terminal errors must not burn attempts
    assert service._refresh_terminal is True
    assert service.status()["state"] == "needs_relogin"
    with pytest.raises(ConfigurationError):
        await service.ensure_ready()


@pytest.mark.unit
async def test_transient_refresh_error_is_retried_then_succeeds(service, fake_http, monkeypatch):
    """A transient 500/429 burns a retry attempt, not the refresh chain."""
    _write_record(service, expires_at=int(time.time()) + 3600)
    new_token = _jwt(_claims(email="retried@example.com"))
    statuses = iter([500, 429])
    slept = []

    async def _record_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(auth_module.asyncio, "sleep", _record_sleep)

    def handler(method, url, data=None):
        try:
            return _FakeResponse(status_code=next(statuses), payload={})
        except StopIteration:
            return _FakeResponse(payload={
                "access_token": new_token,
                "refresh_token": "rt-new",
                "id_token": _jwt(_claims()),
                "expires_in": 10 * 86400,
            })

    fake_http(handler)
    assert await service.get_access_token() == new_token
    assert slept == [1, 2]                  # linear backoff, like the official CLI
    assert service._refresh_terminal is False


@pytest.mark.unit
async def test_persistent_transient_error_exhausts_attempts(service, fake_http, monkeypatch):
    _write_record(service, expires_at=int(time.time()) + 3600)
    calls = []

    async def _skip_sleep(_seconds):
        pass

    monkeypatch.setattr(auth_module.asyncio, "sleep", _skip_sleep)

    def handler(method, url, data=None):
        calls.append(1)
        return _FakeResponse(status_code=500, payload={"error": "server_error"})

    fake_http(handler)
    with pytest.raises(ChatGPTAuthError):
        await service.get_access_token()
    assert len(calls) == auth_module.REFRESH_ATTEMPTS
    assert service._refresh_terminal is False


@pytest.mark.unit
async def test_non_transient_refresh_error_fails_without_retry(service, fake_http, monkeypatch):
    """Only transient statuses burn attempts; a hard 4xx surfaces at once."""

    async def _forbid_sleep(_seconds):
        raise AssertionError("non-transient failures must not back off")

    monkeypatch.setattr(auth_module.asyncio, "sleep", _forbid_sleep)
    _write_record(service, expires_at=int(time.time()) + 3600)
    calls = []

    def handler(method, url, data=None):
        calls.append(1)
        return _FakeResponse(status_code=403, payload={"error": "forbidden"})

    fake_http(handler)
    with pytest.raises(ChatGPTAuthError, match="HTTP 403"):
        await service.get_access_token()
    assert len(calls) == 1
    assert service._refresh_terminal is False


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_ensure_ready_without_login_raises_configuration_error(service):
    with pytest.raises(ConfigurationError):
        await service.ensure_ready()


@pytest.mark.unit
async def test_ensure_ready_with_valid_login_passes(service, fake_http):
    _write_record(service)

    def handler(method, url, data=None):
        raise AssertionError("no HTTP expected while outside the margin")

    fake_http(handler)
    await service.ensure_ready()


# ---------------------------------------------------------------------------
# Import (Codex CLI auth.json)
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_import_codex_nested_format(service):
    nested = {
        "OPENAI_API_KEY": None,
        "last_refresh": "2026-10-01T00:00:00",
        "tokens": {
            "id_token": _jwt(_claims(email="cli@example.com", plan="team")),
            "access_token": _jwt(_claims()),
            "refresh_token": "rt-cli",
            "account_id": "acct-123",
        },
    }
    outcome = await service.import_auth_json(json.dumps(nested))
    assert outcome["status"] == "success"
    assert outcome["email"] == "cli@example.com"
    assert outcome["plan_type"] == "team"
    stored = json.loads(auth_module.CHATGPT_AUTH_FILE.read_text())
    assert stored["refresh_token"] == "rt-cli"
    # The mirror exists immediately — the next LLM call must not need a login.
    mirror = json.loads((auth_module.CHATGPT_TOKEN_DIR / "auth.json").read_text())
    assert mirror["refresh_token"] == "rt-cli"


@pytest.mark.unit
async def test_import_flat_format_with_ms_expiry(service):
    flat = {
        "access_token": _jwt(_claims()),
        "refresh_token": "rt-flat",
        "expires_at": int(time.time() + 86400) * 1000,  # ms epoch
    }
    outcome = await service.import_auth_json(json.dumps(flat))
    assert outcome["status"] == "success"
    stored = json.loads(auth_module.CHATGPT_AUTH_FILE.read_text())
    assert stored["expires_at"] > time.time() + 86000


@pytest.mark.unit
async def test_import_rejects_incomplete_file(service):
    with pytest.raises(ChatGPTAuthError):
        await service.import_auth_json(json.dumps({"access_token": "x"}))
    with pytest.raises(ChatGPTAuthError):
        await service.import_auth_json("not json")


# ---------------------------------------------------------------------------
# Wham endpoints (usage, reset credits)
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_fetch_usage_hits_usage_endpoint(service, fake_http):
    _write_record(service)
    seen = {}

    def handler(method, url, **kwargs):
        seen["method"], seen["url"] = method, url
        return _FakeResponse(200, {"rate_limits": {"primary": {"used_percent": 1}}})

    fake_http(handler)
    payload = await service.fetch_usage()
    assert payload["rate_limits"]["primary"]["used_percent"] == 1
    assert (seen["method"], seen["url"]) == ("GET", f"{auth_module.CHATGPT_API_BASE}/wham/usage")


@pytest.mark.unit
async def test_fetch_reset_credits_hits_reset_credits_endpoint(service, fake_http):
    _write_record(service)
    seen = {}

    def handler(method, url, **kwargs):
        seen["method"], seen["url"] = method, url
        return _FakeResponse(
            200,
            {"available_count": 2, "credits": [
                {"id": "c1", "status": "available", "reset_type": "codex_rate_limits",
                 "granted_at": "2026-10-01T00:00:00Z", "expires_at": None},
            ]},
        )

    fake_http(handler)
    payload = await service.fetch_reset_credits()
    assert payload["available_count"] == 2
    assert (seen["method"], seen["url"]) == (
        "GET", f"{auth_module.CHATGPT_API_BASE}/wham/rate-limit-reset-credits",
    )


@pytest.mark.unit
async def test_consume_reset_credit_posts_idempotency_key(service, fake_http):
    _write_record(service)
    bodies = []

    def handler(method, url, headers=None, json_body=None):
        bodies.append((method, url, json_body))
        return _FakeResponse(200, {"code": "reset", "windows_reset": 2})

    fake_http(handler)
    outcome = await service.consume_reset_credit(credit_id="card-1")
    assert outcome == {"code": "reset", "windows_reset": 2}
    method, url, body = bodies[0]
    assert (method, url) == (
        "POST", f"{auth_module.CHATGPT_API_BASE}/wham/rate-limit-reset-credits/consume",
    )
    assert body["credit_id"] == "card-1"
    first_key = body["redeem_request_id"]

    # A second redemption mints a fresh idempotency key — two consecutive
    # redemptions are independent operations, not one deduplicated request.
    await service.consume_reset_credit()
    assert bodies[1][2].get("credit_id") is None
    assert bodies[1][2]["redeem_request_id"] != first_key


@pytest.mark.unit
async def test_wham_request_auth_and_error_mapping(service, fake_http):
    _write_record(service)
    headers_seen = {}

    def ok_handler(method, url, headers=None, json_body=None):
        headers_seen.update(headers or {})
        return _FakeResponse(200, {})

    fake_http(ok_handler)
    await service.fetch_usage()
    assert headers_seen["Authorization"].startswith("Bearer ")
    assert headers_seen["chatgpt-account-id"] == "acct-123"
    assert headers_seen["originator"] == auth_module.CHATGPT_ORIGINATOR
    assert headers_seen["User-Agent"] == auth_module.CHATGPT_USER_AGENT

    fake_http(lambda *a, **k: _FakeResponse(401, {}))
    with pytest.raises(ChatGPTAuthError, match="sign in again"):
        await service.fetch_reset_credits()

    fake_http(lambda *a, **k: _FakeResponse(503, {}))
    with pytest.raises(ChatGPTAuthError, match="redemption"):
        await service.consume_reset_credit()


# ---------------------------------------------------------------------------
# Status / logout
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_status_states(service):
    assert service.status()["state"] == "logged_out"

    _write_record(service)
    status = service.status()
    assert status["state"] == "active"
    assert status["email"] == "user@example.com"
    assert status["plan_type"] == "plus"


@pytest.mark.unit
def test_status_recovers_plan_from_nested_claim(service):
    """Codex tokens nest chatgpt_plan_type in the auth claim; a record with
    no plan field (e.g. persisted by an older extraction) still yields the
    plan instead of hiding the tier badge."""
    nested = {
        "email": "user@example.com",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-123",
            "chatgpt_plan_type": "plus",
        },
        "exp": int(time.time()) + 86400,
    }
    service._persist({
        "access_token": _jwt(nested),
        "refresh_token": "rt",
        "id_token": _jwt(nested),
        "expires_at": int(time.time()) + 5 * 86400,
        "account_id": "acct-123",
        "email": "user@example.com",
        "updated_at": int(time.time()),
    })
    assert service.status()["plan_type"] == "plus"


@pytest.mark.unit
async def test_logout_removes_token_files(service):
    _write_record(service)
    outcome = await service.logout()
    assert outcome["status"] == "success"
    assert not auth_module.CHATGPT_AUTH_FILE.exists()
    assert not (auth_module.CHATGPT_TOKEN_DIR / "auth.json").exists()
    assert service.status()["state"] == "logged_out"


# ---------------------------------------------------------------------------
# Interactive login flow (paste path, embedded Chrome stubbed out)
# ---------------------------------------------------------------------------

class _StubReaper:
    def __init__(self):
        self.suspended = 0

    def suspend(self):
        self.suspended += 1

    def resume(self):
        self.suspended -= 1


@pytest.fixture
def stub_browser(monkeypatch):
    """Stub the embedded-Chrome leg and the reaper for flow tests."""
    stub = _StubReaper()
    import app.agents.tools.browser_thread as browser_thread
    import app.agents.tools.browser_tab_reaper as reaper_module
    import app.services.browser_service as browser_service

    async def _no_tab(coro):
        coro.close()
        raise RuntimeError("no chrome in tests")

    class _FakeBrowserService:
        async def get_status(self):
            return {"status": "stopped"}

        async def start(self):
            raise RuntimeError("no chrome in tests")

    monkeypatch.setattr(browser_thread, "dispatch", _no_tab)
    monkeypatch.setattr(reaper_module, "get_tab_reaper", lambda: stub)
    monkeypatch.setattr(browser_service, "get_browser_service", lambda: _FakeBrowserService())
    return stub


@pytest.mark.unit
async def test_paste_login_flow_end_to_end(service, fake_http, stub_browser, monkeypatch):
    async def _no_listener(session):
        return None
    monkeypatch.setattr(service, "_start_listener", _no_listener)

    def handler(method, url, data=None):
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "auth-code-1"
        assert data["code_verifier"] == service._login.verifier
        return _FakeResponse(payload={
            "access_token": _jwt(_claims(email="flow@example.com", plan="plus")),
            "refresh_token": "rt-flow",
            "id_token": _jwt(_claims(email="flow@example.com", plan="plus")),
            "expires_in": 10 * 86400,
        })

    fake_http(handler)

    session = await service.start_login()
    try:
        outcome = await service.complete_callback_url(
            f"http://localhost:1455/auth/callback?code=auth-code-1&state={session.state}"
        )
    finally:
        await asyncio.gather(session.task, return_exceptions=True)

    assert outcome["status"] == "success"
    assert outcome["email"] == "flow@example.com"
    assert json.loads(auth_module.CHATGPT_AUTH_FILE.read_text())["refresh_token"] == "rt-flow"
    assert service.status()["state"] == "active"
    # Reaper was suspended and fully released by the flow's finally block.
    assert stub_browser.suspended == 0
    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait()[0])
    assert events[0] == "login_start"
    assert "login_error" not in events
    assert events[-1] == "login_done"


@pytest.mark.unit
async def test_paste_login_rejects_state_mismatch(service, stub_browser, monkeypatch):
    async def _no_listener(session):
        return None
    monkeypatch.setattr(service, "_start_listener", _no_listener)

    session = await service.start_login()
    try:
        with pytest.raises(ChatGPTAuthError):
            await service.complete_callback_url(
                "http://localhost:1455/auth/callback?code=x&state=wrong-state"
            )
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)


@pytest.mark.unit
async def test_second_login_rejected_while_active(service, stub_browser, monkeypatch):
    async def _no_listener(session):
        return None
    monkeypatch.setattr(service, "_start_listener", _no_listener)

    session = await service.start_login()
    try:
        with pytest.raises(ChatGPTAuthError):
            await service.start_login()
    finally:
        await service.cancel_login()


@pytest.mark.unit
async def test_manual_login_skips_browser(service, stub_browser, monkeypatch):
    """open_browser=False leaves the embedded Chrome alone — no browser stack
    startup, no tab. The user authorizes in their own browser and pastes."""
    import app.agents.tools.browser_thread as browser_thread
    import app.services.browser_service as browser_service

    async def _forbidden_dispatch(coro):
        coro.close()
        raise AssertionError("manual login must not touch the browser")

    def _forbidden_service():
        raise AssertionError("manual login must not start the browser stack")

    monkeypatch.setattr(browser_thread, "dispatch", _forbidden_dispatch)
    monkeypatch.setattr(browser_service, "get_browser_service", _forbidden_service)

    async def _no_listener(session):
        return None
    monkeypatch.setattr(service, "_start_listener", _no_listener)

    session = await service.start_login(open_browser=False)
    try:
        # One loop tick: let the flow task run its synchronous prefix so the
        # event sequence below reflects an actually started flow.
        await asyncio.sleep(0)
        assert session.open_browser is False
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait()[0])
    assert events[0] == "login_start"
    assert "login_browser" not in events


@pytest.mark.unit
async def test_status_exposes_pending_login_details(service, fake_http, stub_browser, monkeypatch):
    """While a login is in flight, status carries its authorize URL and
    browser mode — a page refresh (which drops the SSE stream) can restore
    the running-login view from these fields alone."""
    async def _no_listener(session):
        return None
    monkeypatch.setattr(service, "_start_listener", _no_listener)

    def handler(method, url, data=None):
        return _FakeResponse(payload={
            "access_token": _jwt(_claims(email="status@example.com")),
            "refresh_token": "rt-status",
            "id_token": _jwt(_claims(email="status@example.com")),
            "expires_in": 10 * 86400,
        })

    fake_http(handler)
    session = await service.start_login(open_browser=False)
    pending = service.status()
    assert pending["login_pending"] is True
    assert pending["login_authorize_url"] == session.authorize_url
    assert pending["login_open_browser"] is False

    try:
        await service.complete_callback_url(
            f"http://localhost:1455/auth/callback?code=auth-code-2&state={session.state}"
        )
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)

    final = service.status()
    assert final["login_pending"] is False
    assert "login_authorize_url" not in final
    assert "login_open_browser" not in final
    assert final["state"] == "active"


@pytest.mark.unit
async def test_import_supersedes_pending_login(service, fake_http, stub_browser, monkeypatch):
    """Importing tokens while a login is running stops that login before
    persisting, so a concurrently completing code exchange cannot overwrite
    the imported record."""
    async def _no_listener(session):
        return None
    monkeypatch.setattr(service, "_start_listener", _no_listener)

    session = await service.start_login()
    try:
        outcome = await service.import_auth_json(json.dumps({
            "access_token": _jwt(_claims(email="imported@example.com", plan="pro")),
            "refresh_token": "rt-imported",
        }))
        assert outcome["status"] == "success"

        assert service.status()["login_pending"] is False
        stored = json.loads(auth_module.CHATGPT_AUTH_FILE.read_text())
        assert stored["refresh_token"] == "rt-imported"
        assert stored["email"] == "imported@example.com"
    finally:
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)


@pytest.mark.unit
async def test_callback_listener_over_real_http(service, fake_http, stub_browser):
    """The loopback listener must accept the redirect request and complete
    the flow without any paste step."""

    def handler(method, url, data=None):
        return _FakeResponse(payload={
            "access_token": _jwt(_claims(email="listener@example.com")),
            "refresh_token": "rt-listener",
            "id_token": _jwt(_claims(email="listener@example.com")),
            "expires_in": 10 * 86400,
        })

    # Capture the real transport class before the fixture's fake client takes
    # over the module attribute — the poll below must hit the real listener.
    real_async_client = httpx.AsyncClient
    fake_http(handler)
    session = await service.start_login()
    try:
        state = session.state
        # The listener binds inside the flow task — poll until it accepts.
        # The request must be async: a blocking call would freeze the very
        # loop the listener handler needs to answer.
        resp = None
        deadline = time.monotonic() + 10
        async with real_async_client(timeout=5) as client:
            while time.monotonic() < deadline:
                try:
                    resp = await client.get(
                        f"http://127.0.0.1:1455/auth/callback?code=real-code&state={state}",
                    )
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
        assert resp is not None, "callback listener never came up"
        assert resp.status_code == 200
        assert "SiGMA" in resp.text
        await asyncio.wait_for(session.task, timeout=15)
    finally:
        if not session.task.done():
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)

    assert session.outcome["status"] == "success"
    assert session.outcome["email"] == "listener@example.com"
    assert stub_browser.suspended == 0
