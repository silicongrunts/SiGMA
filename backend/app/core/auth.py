"""
Access-control primitives for SiGMA.

Centralized, testable helpers for the shared-password access gate:

- ``hash_password`` / ``verify_password`` — bcrypt password hashing/verification.
- ``get_auth_secret`` — lazily provisions the HMAC signing secret used to sign
  session cookies. The secret lives in ``userdata/.SiGMA/auth_secret.key`` and
  is rotated whenever the password changes, which immediately invalidates every
  previously issued cookie.
- ``session_token`` — a deterministic, unforgeable token derived from the
  password hash and the signing secret. It is what the browser stores; it is
  neither the hash nor the plaintext password, and it changes whenever the
  password changes.
- ``is_authenticated`` / ``set_session_cookie`` / ``clear_session_cookie`` —
  request inspection and response cookie management.
- ``AUTH_PUBLIC_PATHS`` — the allow-list of paths reachable without a valid
  cookie when a password is set.

Security notes
--------------
- The plaintext password is never persisted or logged.
- Cookies are ``HttpOnly`` + ``SameSite=Lax``; ``Secure`` is added when the
  request was served over HTTPS.
- Token = ``sha256(hmac(secret, password_hash))``. Because it is keyed by the
  secret, an attacker who only knows the bcrypt hash cannot forge a token, and
  rotating the secret on password change invalidates all outstanding cookies.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets as _secrets
from pathlib import Path
from typing import Optional

from fastapi import Request, Response
from starlette.datastructures import Headers

import bcrypt

from app.core.config import SIGMA_DIR, settings

# Cookie lifetime: 365 days, per the product requirement.
SESSION_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
SESSION_COOKIE_NAME = "sigma_session"

# bcrypt work factor. 12 is a deliberate slow-down against offline guessing on
# an exposed LAN; combined with the opt-out of in-app rate limiting it is the
# sole brute-force mitigation.
BCRYPT_COST = 12

# Password length bounds — shared by the /auth/password route and the offline
# reset_password script so both reject weak passwords identically.
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 256

# Paths reachable without a valid session cookie when a password is set.
# Keep this list narrow — every entry is an unauthenticated surface.
AUTH_PUBLIC_PATHS: frozenset[str] = frozenset({
    "/api/v1/auth/status",
    "/api/v1/auth/login",
    "/api/health",
})

_AUTH_SECRET_PATH = SIGMA_DIR / "auth_secret.key"
_auth_secret_cache: Optional[bytes] = None


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain* (cost ``BCRYPT_COST``)."""
    salt = bcrypt.gensalt(rounds=BCRYPT_COST)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    """Return True if *plain* matches *password_hash*.

    Constant-time comparison is handled by bcrypt internally. A malformed or
    empty stored hash never verifies — fail closed.
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Signing secret
# ---------------------------------------------------------------------------

def get_auth_secret() -> bytes:
    """Return the HMAC signing secret, provisioning it on first use.

    The secret is 32 cryptographically-random bytes persisted to
    ``auth_secret.key`` with mode 0600. It is cached in-process for the lifetime
    of the worker. Rotating it (deleting the file or calling
    :func:`rotate_auth_secret`) invalidates all outstanding session cookies.
    """
    global _auth_secret_cache
    if _auth_secret_cache is not None:
        return _auth_secret_cache

    SIGMA_DIR.mkdir(parents=True, exist_ok=True)
    if _AUTH_SECRET_PATH.exists():
        data = _AUTH_SECRET_PATH.read_bytes().strip()
        if len(data) >= 32:
            _auth_secret_cache = data
            return data

    data = _secrets.token_bytes(32)
    _write_secret(data)
    _auth_secret_cache = data
    return data


def rotate_auth_secret() -> bytes:
    """Generate and persist a fresh signing secret, invalidating all cookies.

    Called whenever the access password changes so that previously issued
    cookies stop validating immediately.
    """
    global _auth_secret_cache
    data = _secrets.token_bytes(32)
    _write_secret(data)
    _auth_secret_cache = data
    return data


def _write_secret(data: bytes) -> None:
    """Persist *data* as the signing secret with restrictive permissions."""
    SIGMA_DIR.mkdir(parents=True, exist_ok=True)
    # Write via a temp file + rename for atomicity, then lock down permissions.
    tmp = _AUTH_SECRET_PATH.with_suffix(".key.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, _AUTH_SECRET_PATH)
    os.chmod(_AUTH_SECRET_PATH, 0o600)


# ---------------------------------------------------------------------------
# Session token
# ---------------------------------------------------------------------------

def session_token(password_hash: str) -> str:
    """Derive the session token for a given password hash.

    The token is keyed by the signing secret, so it cannot be forged from the
    hash alone, and it rotates with the secret. It is stable for a given
    (secret, password) pair, so it validates repeatedly without server-side
    session storage.
    """
    secret = get_auth_secret()
    mac = hmac.new(secret, password_hash.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def password_enabled() -> bool:
    """Return True if an access password is currently configured."""
    return bool(settings.security.password_hash)


# ---------------------------------------------------------------------------
# Request inspection & response cookie management
# ---------------------------------------------------------------------------

def _cookie_from_scope(scope: dict) -> str:
    """Extract the session cookie value from an ASGI scope.

    Works for both ``http`` and ``websocket`` scopes — they carry headers in
    the same ``list[tuple[bytes, bytes]]`` shape. The cookie header is parsed
    with the same forgiving logic Starlette uses for ``Request.cookies`` so the
    two paths agree.
    """
    from starlette.requests import cookie_parser

    cookie_header = Headers(scope=scope).get("cookie")
    if not cookie_header:
        return ""
    return cookie_parser(cookie_header).get(SESSION_COOKIE_NAME, "")


def is_authenticated_scope(scope: dict) -> bool:
    """Return True if *scope* (an ASGI scope) carries a valid session cookie.

    Unlike :func:`is_authenticated`, this does not require a ``Request`` object
    so it can be used directly inside a pure-ASGI middleware that also handles
    WebSocket connections (``BaseHTTPMiddleware`` never invokes ``dispatch``
    for ``websocket`` scopes, so a request-level check alone would let
    WebSockets bypass the gate).
    """
    if not password_enabled():
        return True
    token = _cookie_from_scope(scope)
    if not token:
        return False
    expected = session_token(settings.security.password_hash)
    return hmac.compare_digest(token, expected)


def is_authenticated(request: Request) -> bool:
    """Return True if *request* carries a valid session cookie."""
    return is_authenticated_scope(request.scope)


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    """Attach a session cookie to *response* with hardened flags."""
    secure = request.url.scheme == "https" or _is_behind_secure_proxy(request)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    """Delete the session cookie from *response*."""
    secure = request.url.scheme == "https" or _is_behind_secure_proxy(request)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _is_behind_secure_proxy(request: Request) -> bool:
    """Detect an HTTPS request proxied over plain HTTP (nginx termination)."""
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return forwarded_proto == "https"


# ---------------------------------------------------------------------------
# Public-path policy
# ---------------------------------------------------------------------------

def is_public_path(path: str) -> bool:
    """Return True if *path* may be served without a session cookie."""
    if path in AUTH_PUBLIC_PATHS:
        return True
    # Frontend static assets are needed to render the login screen itself.
    if path.startswith("/assets/"):
        return True
    return False


def is_password_change_path(path: str) -> bool:
    """Return True for the password-management endpoint.

    Unlike the other public paths, ``/auth/password`` is conditionally public:
    it is open when no password is set (first-time setup) and auth-protected
    once one exists. The middleware treats it as public; the route enforces the
    authenticated case.
    """
    return path == "/api/v1/auth/password"
