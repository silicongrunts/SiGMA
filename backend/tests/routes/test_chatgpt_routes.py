"""
Tests for the /chatgpt route adapters.

The handlers are thin HTTP adapters over ``chatgpt_auth_service`` (whose
logic is covered by its own test file), so these tests pin the adapter
contracts: exception-to-HTTP-status mapping, the SSE frame format consumed
by the frontend parser, the ``in_use`` merge into the status payload, and
request-body adaptation.
"""

import pytest
from fastapi import HTTPException

from app.core.config import ModelSettings, settings
from app.core.exceptions import ChatGPTAuthError
from app.models.requests import (
    ChatGPTCallbackRequest,
    ChatGPTImportRequest,
    ChatGPTLoginRequest,
    ChatGPTResetCreditRequest,
)
from app.routes import chatgpt as chatgpt_routes

pytestmark = [pytest.mark.route]


class _StubAuthService:
    """chatgpt_auth_service stand-in: one configurable error, canned results."""

    def __init__(self):
        self.error = None
        self.consumed_credit_id = "unset"
        self.login_open_browser = "unset"

    def status(self):
        return {"state": "active", "email": "user@example.com"}

    async def start_login(self, open_browser=True):
        if self.error:
            raise self.error
        self.login_open_browser = open_browser
        return object()

    async def stream_login_events(self):
        yield "login_start", {"authorize_url": "https://auth.example"}
        yield "login_done", {}

    async def complete_callback_url(self, url):
        if self.error:
            raise self.error
        return {"status": "success"}

    async def import_auth_json(self, content):
        if self.error:
            raise self.error
        return {"status": "success"}

    async def logout(self):
        if self.error:
            raise self.error
        return {"status": "success"}

    async def fetch_usage(self):
        if self.error:
            raise self.error
        return {"rate_limits": {}}

    async def fetch_reset_credits(self):
        if self.error:
            raise self.error
        return {"available_count": 0}

    async def consume_reset_credit(self, credit_id=None):
        if self.error:
            raise self.error
        self.consumed_credit_id = credit_id
        return {"code": "reset"}


@pytest.mark.asyncio
async def test_status_merges_subscription_in_use(monkeypatch):
    """The status payload reports whether any transport role resolves to the
    chatgpt provider — that flag drives the subscription UI outside settings."""
    stub = _StubAuthService()
    monkeypatch.setattr(chatgpt_routes, "chatgpt_auth_service", stub)
    for role in ("supervisor", "ra", "vision", "draw"):
        monkeypatch.setattr(settings.models, role, ModelSettings(model="m", provider="openai"))

    body = await chatgpt_routes.get_status()
    assert body["data"]["in_use"] is False
    assert body["data"]["state"] == "active"

    monkeypatch.setattr(settings.models, "ra", ModelSettings(
        model="gpt-5.3-codex", provider="chatgpt",
    ))
    body = await chatgpt_routes.get_status()
    assert body["data"]["in_use"] is True


@pytest.mark.asyncio
async def test_login_conflict_maps_to_409(monkeypatch):
    stub = _StubAuthService()
    stub.error = ChatGPTAuthError("A ChatGPT login is already in progress.")
    monkeypatch.setattr(chatgpt_routes, "chatgpt_auth_service", stub)

    with pytest.raises(HTTPException) as exc_info:
        await chatgpt_routes.start_login(ChatGPTLoginRequest())
    assert exc_info.value.status_code == 409
    assert "already in progress" in exc_info.value.detail


@pytest.mark.asyncio
async def test_login_stream_emits_sse_frames(monkeypatch):
    """The stream body must be parseable by the shared SSE helper: one
    ``event:`` line plus one JSON ``data:`` line per frame, blank-line
    terminated."""
    stub = _StubAuthService()
    monkeypatch.setattr(chatgpt_routes, "chatgpt_auth_service", stub)

    response = await chatgpt_routes.start_login(ChatGPTLoginRequest())
    frames = [chunk async for chunk in response.body_iterator]

    assert frames[0] == 'event: login_start\ndata: {"authorize_url": "https://auth.example"}\n\n'
    assert frames[-1] == "event: login_done\ndata: {}\n\n"


@pytest.mark.asyncio
async def test_auth_error_to_http_status_mapping(monkeypatch):
    """Each adapter translates ChatGPTAuthError to its own HTTP status:
    400 for caller-fixable inputs, 422 for an unusable upload, 500 when
    signing out fails server-side."""
    stub = _StubAuthService()
    stub.error = ChatGPTAuthError("boom")
    monkeypatch.setattr(chatgpt_routes, "chatgpt_auth_service", stub)

    cases = [
        (chatgpt_routes.deliver_callback, ChatGPTCallbackRequest(url="http://localhost:1455/auth/callback"), 400),
        (chatgpt_routes.import_auth_file, ChatGPTImportRequest(content="{}"), 422),
        (chatgpt_routes.get_usage, None, 400),
        (chatgpt_routes.get_reset_credits, None, 400),
        (chatgpt_routes.consume_reset_credit, ChatGPTResetCreditRequest(), 400),
        (chatgpt_routes.logout, None, 500),
    ]
    for handler, payload, expected_status in cases:
        with pytest.raises(HTTPException) as exc_info:
            if payload is None:
                await handler()
            else:
                await handler(payload)
        assert exc_info.value.status_code == expected_status, handler.__name__


@pytest.mark.asyncio
async def test_reset_credit_request_adaptation(monkeypatch):
    """An omitted credit_id reaches the service as None (backend picks an
    eligible card); a given one passes through."""
    stub = _StubAuthService()
    monkeypatch.setattr(chatgpt_routes, "chatgpt_auth_service", stub)

    await chatgpt_routes.consume_reset_credit(ChatGPTResetCreditRequest(credit_id="card-1"))
    assert stub.consumed_credit_id == "card-1"

    await chatgpt_routes.consume_reset_credit(ChatGPTResetCreditRequest())
    assert stub.consumed_credit_id is None


@pytest.mark.asyncio
async def test_login_request_adaptation(monkeypatch):
    """open_browser rides through to the service; default opens the embedded
    browser, false selects the user's own browser (paste mode)."""
    stub = _StubAuthService()
    monkeypatch.setattr(chatgpt_routes, "chatgpt_auth_service", stub)

    await chatgpt_routes.start_login(ChatGPTLoginRequest())
    assert stub.login_open_browser is True

    await chatgpt_routes.start_login(ChatGPTLoginRequest(open_browser=False))
    assert stub.login_open_browser is False
