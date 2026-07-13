"""
Tests for the access-control primitives in app.core.auth.

These cover the security-critical invariants:
- bcrypt hash/verify round-trip.
- empty or malformed stored hashes never verify (fail closed).
- the session token rotates when the password hash changes.
"""

import pytest

from app.core import auth
from app.core.auth import (
    SESSION_COOKIE_NAME,
    hash_password,
    is_public_path,
    session_token,
    verify_password,
)


def test_hash_and_verify_round_trip():
    h = hash_password("s3cret-pass")
    assert h != "s3cret-pass"
    assert verify_password("s3cret-pass", h) is True
    assert verify_password("wrong", h) is False


def test_empty_hash_never_verifies():
    # No password configured must mean "never authenticated by password".
    assert verify_password("anything", "") is False
    assert verify_password("", "") is False


def test_malformed_hash_never_verifies():
    # A garbage string in the hash field must not raise; it must just fail.
    assert verify_password("anything", "not-a-real-hash") is False


def test_session_token_changes_when_password_changes(monkeypatch):
    # Force a stable signing secret so the token delta is driven by the hash.
    monkeypatch.setattr(auth, "_auth_secret_cache", b"x" * 32)
    h1 = hash_password("first-password")
    h2 = hash_password("second-password")
    t1 = session_token(h1)
    t2 = session_token(h2)
    assert t1 != t2
    # Same inputs are stable (no server-side session store needed).
    assert session_token(h1) == t1


def test_session_token_is_deterministic_for_same_inputs(monkeypatch):
    monkeypatch.setattr(auth, "_auth_secret_cache", b"k" * 32)
    h = hash_password("pw")
    assert session_token(h) == session_token(h)


def test_is_public_path():
    assert is_public_path("/api/v1/auth/status") is True
    assert is_public_path("/api/v1/auth/login") is True
    assert is_public_path("/api/health") is True
    assert is_public_path("/assets/index-abc.js") is True
    # Everything else requires auth.
    assert is_public_path("/api/v1/projects") is False
    assert is_public_path("/") is False
    assert is_public_path("/api/v1/system/settings") is False


def test_get_auth_secret_is_stable_and_random(monkeypatch, tmp_path):
    # Point the secret path at a temp location and clear the cache.
    monkeypatch.setattr(auth, "_AUTH_SECRET_PATH", tmp_path / "auth_secret.key")
    monkeypatch.setattr(auth, "_auth_secret_cache", None)
    s1 = auth.get_auth_secret()
    s2 = auth.get_auth_secret()
    assert s1 == s2  # cached / persisted
    assert len(s1) == 32


def test_rotate_auth_secret_invalidates_prior_token(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_AUTH_SECRET_PATH", tmp_path / "auth_secret.key")
    monkeypatch.setattr(auth, "_auth_secret_cache", None)
    h = hash_password("pw")
    before = session_token(h)
    auth.rotate_auth_secret()
    after = session_token(h)
    assert before != after  # secret rotation changes the token


# ---------------------------------------------------------------------------
# ASGI scope-level authentication (used by the pure-ASGI AuthMiddleware)
# ---------------------------------------------------------------------------

def _scope(headers=None, scope_type="http", path="/", method="GET"):
    """Build a minimal ASGI scope for testing."""
    return {
        "type": scope_type,
        "path": path,
        "method": method,
        "headers": [(k.encode(), v.encode()) for k, v in (headers or [])],
    }


def test_is_authenticated_scope_passes_when_no_password(monkeypatch):
    monkeypatch.setattr(auth.settings.security, "password_hash", "")
    scope = _scope()
    assert auth.is_authenticated_scope(scope) is True


def test_is_authenticated_scope_rejects_missing_cookie(monkeypatch):
    monkeypatch.setattr(auth.settings.security, "password_hash", hash_password("s3cret"))
    assert auth.is_authenticated_scope(_scope()) is False


def test_is_authenticated_scope_rejects_wrong_cookie(monkeypatch):
    monkeypatch.setattr(auth.settings.security, "password_hash", hash_password("s3cret"))
    scope = _scope([("cookie", f"{SESSION_COOKIE_NAME}=forged")])
    assert auth.is_authenticated_scope(scope) is False


def test_is_authenticated_scope_accepts_valid_cookie(monkeypatch):
    monkeypatch.setattr(auth.settings.security, "password_hash", hash_password("s3cret"))
    monkeypatch.setattr(auth, "_auth_secret_cache", b"k" * 32)
    token = session_token(auth.settings.security.password_hash)
    scope = _scope([("cookie", f"{SESSION_COOKIE_NAME}={token}")])
    assert auth.is_authenticated_scope(scope) is True


def test_is_authenticated_scope_works_for_websocket_scope(monkeypatch):
    """The scope-level check must work for websocket scopes too — this is the
    whole reason it exists (BaseHTTPMiddleware never sees websocket scopes)."""
    monkeypatch.setattr(auth.settings.security, "password_hash", hash_password("s3cret"))
    monkeypatch.setattr(auth, "_auth_secret_cache", b"k" * 32)
    token = session_token(auth.settings.security.password_hash)
    ws_scope = _scope(
        [("cookie", f"{SESSION_COOKIE_NAME}={token}")],
        scope_type="websocket",
        path="/api/v1/terminal/proj",
    )
    assert auth.is_authenticated_scope(ws_scope) is True
    # Without a cookie the websocket scope must also be rejected.
    assert auth.is_authenticated_scope(
        _scope(scope_type="websocket", path="/api/v1/terminal/proj")
    ) is False
