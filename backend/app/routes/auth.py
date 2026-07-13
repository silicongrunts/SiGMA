"""
Access-control (authentication) routes.

Endpoints
---------
- ``GET  /auth/status``   — public; reports whether a password is configured.
- ``POST /auth/login``    — public; verifies the password and sets the cookie.
- ``POST /auth/logout``   — clears the cookie.
- ``POST /auth/password`` — changes the access password. Open when no password
  is set (first-time setup); auth-protected once one exists. Rotates the signing
  secret so all prior cookies immediately stop validating.

The plaintext password is never persisted or logged.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.core.auth import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    clear_session_cookie,
    hash_password,
    is_authenticated,
    password_enabled,
    rotate_auth_secret,
    session_token,
    set_session_cookie,
    verify_password,
)
from app.core.config import settings, update_password_hash
from app.core.exceptions import AuthenticationError
from app.core.response import ok

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1)


class PasswordChangeRequest(BaseModel):
    new_password: str = Field(..., max_length=MAX_PASSWORD_LENGTH)


@router.get("/status")
async def auth_status():
    """Report whether access protection is active. Always public."""
    return ok({"password_enabled": password_enabled()})


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response):
    """Verify the password and issue a session cookie on success."""
    if not password_enabled():
        # No password configured — nothing to log in to.
        return ok({"password_enabled": False})

    if not verify_password(body.password, settings.security.password_hash):
        # Deliberately generic: do not reveal whether a password is configured
        # to an unauthenticated caller beyond the public /status endpoint.
        raise AuthenticationError("Incorrect password")

    token = session_token(settings.security.password_hash)
    set_session_cookie(response, token, request)
    logger.info("Successful login")
    return ok({"password_enabled": True})


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Clear the session cookie."""
    clear_session_cookie(response, request)
    return ok({"status": "logged_out"})


@router.post("/password")
async def change_password(body: PasswordChangeRequest, request: Request, response: Response):
    """Set, change, or clear the access password.

    - When a password is currently set, the caller must already be authenticated
      (valid cookie); otherwise this endpoint 401s. This prevents a logged-out
      user on the LAN from changing the password to take over the instance.
    - When no password is set, this endpoint is open so the first password can
      be established from the settings panel.

    Setting ``new_password`` to an empty string clears the password entirely
      (disabling access protection).
    """
    if password_enabled() and not is_authenticated(request):
        raise AuthenticationError("Authentication required to change the password")

    new_password = body.new_password

    if new_password:
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
            )
        new_hash = hash_password(new_password)
        update_password_hash(new_hash)
        # Rotate the signing secret so every outstanding cookie — including the
        # caller's own — is invalidated, forcing a fresh login. The caller is NOT
        # auto-authenticated here; they must sign in with the new password.
        rotate_auth_secret()
        logger.info("Access password updated")
    else:
        # Clear the password: disable access protection entirely.
        update_password_hash("")
        rotate_auth_secret()
        clear_session_cookie(response, request)
        logger.info("Access password cleared")

    return ok({"password_enabled": bool(new_password)})
