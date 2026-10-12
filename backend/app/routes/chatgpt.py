"""ChatGPT subscription (Codex OAuth) routes.

Thin HTTP adapters over ``chatgpt_auth_service``: login (SSE progress),
callback paste, auth-file import, cancel, status, quota, and logout. All
business logic — token files, the refresh chain, the loopback listener, and
the embedded-Chrome tab — lives in the service.
"""

import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.core.exceptions import ChatGPTAuthError
from app.core.response import ok
from app.models.requests import (
    ChatGPTCallbackRequest,
    ChatGPTImportRequest,
    ChatGPTLoginRequest,
    ChatGPTResetCreditRequest,
)
from app.services.chatgpt_auth_service import chatgpt_auth_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chatgpt", tags=["chatgpt"])


def _sse(event: str, data: dict) -> str:
    """Format one SSE frame (same contract as the settings check stream)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/status")
async def get_status():
    """Subscription login state plus whether any role currently resolves to
    the chatgpt provider (drives the subscription UI outside the settings).
    While a login is in flight, also carries its authorize URL and whether
    the embedded browser owns it — the running-login view survives page
    refreshes through these fields."""
    from app.core.model_config import chatgpt_subscription_in_use

    return ok({**chatgpt_auth_service.status(), "in_use": chatgpt_subscription_in_use()})


@router.post("/login")
async def start_login(data: ChatGPTLoginRequest):
    """Start an interactive login and stream its progress as SSE events.

    ``open_browser=false`` skips the embedded Chrome (the user opens the
    authorize URL — available from GET /chatgpt/status while pending — in
    their own browser). The flow runs in a service-owned task, so closing
    this connection (or refreshing the page) never aborts the login;
    GET /chatgpt/status shows the eventual outcome.
    """
    try:
        await chatgpt_auth_service.start_login(open_browser=data.open_browser)
    except ChatGPTAuthError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc

    async def stream():
        try:
            async for event, payload in chatgpt_auth_service.stream_login_events():
                yield _sse(event, payload)
        except ChatGPTAuthError as exc:
            # The login ended between start_login and the subscription.
            yield _sse("login_error", {"message": exc.message})
            yield _sse("login_done", {})

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/login/callback")
async def deliver_callback(data: ChatGPTCallbackRequest):
    """Complete a pending login with a pasted OAuth redirect URL."""
    try:
        outcome = await chatgpt_auth_service.complete_callback_url(data.url)
    except ChatGPTAuthError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    return ok(outcome)


@router.post("/login/cancel")
async def cancel_login():
    await chatgpt_auth_service.cancel_login()
    return ok({"status": "cancelled"})


@router.post("/login/import")
async def import_auth_file(data: ChatGPTImportRequest):
    """Import a token file exported by the official Codex CLI (or a flat
    token record). Validates the JWT claims before persisting."""
    try:
        outcome = await chatgpt_auth_service.import_auth_json(data.content)
    except ChatGPTAuthError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    return ok(outcome)


@router.post("/logout")
async def logout():
    """Remove the stored tokens and cancel any pending login."""
    try:
        return ok(await chatgpt_auth_service.logout())
    except ChatGPTAuthError as exc:
        raise HTTPException(status_code=500, detail=exc.message) from exc


@router.get("/usage")
async def get_usage():
    """Current plan rate-limit windows from the Codex usage endpoint."""
    try:
        return ok(await chatgpt_auth_service.fetch_usage())
    except ChatGPTAuthError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.get("/reset-credits")
async def get_reset_credits():
    """Available rate-limit reset credits ("reset cards") for the account."""
    try:
        return ok(await chatgpt_auth_service.fetch_reset_credits())
    except ChatGPTAuthError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc


@router.post("/reset-credits/consume")
async def consume_reset_credit(data: ChatGPTResetCreditRequest):
    """Redeem one reset credit to clear the current rate-limit windows."""
    try:
        return ok(await chatgpt_auth_service.consume_reset_credit(
            credit_id=data.credit_id or None
        ))
    except ChatGPTAuthError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
