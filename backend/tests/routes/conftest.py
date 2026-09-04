"""
Shared fixtures for route-level tests.

HTTP tests drive the real FastAPI app through httpx's ASGITransport so the
full middleware stack (CORS -> AuthMiddleware -> RequestID -> Logging) and
the registered exception handlers run end-to-end.
"""

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.core import auth
from app.core.auth import hash_password
from app.core.config import settings


@pytest.fixture(autouse=True)
def isolate_auth_secret(monkeypatch, tmp_path):
    """Keep every route test off the real userdata/.SiGMA/auth_secret.key.

    Password-change routes call ``rotate_auth_secret()``, which persists the
    signing secret to ``SIGMA_DIR``; without this fixture a test run rewrites
    the real key and kicks every login session of a live instance. Both the
    secret path and the in-process cache are pointed at a per-test temporary
    file (same approach as tests/core/test_auth.py).
    """
    monkeypatch.setattr(auth, "_AUTH_SECRET_PATH", tmp_path / "auth_secret.key")
    monkeypatch.setattr(auth, "_auth_secret_cache", None)


@pytest.fixture
def app():
    """The real FastAPI application singleton."""
    from app.main import app

    return app


@pytest_asyncio.fixture
async def client(app):
    """An httpx client bound to the real app via ASGITransport.

    Tests that need a session put it on the client (not per-request, which
    httpx deprecates): ``client.cookies.set(SESSION_COOKIE_NAME, token)``.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        yield client


@pytest.fixture
def no_password(monkeypatch):
    """Disable the access gate; the previous state is restored afterwards."""
    monkeypatch.setattr(settings.security, "password_hash", "")


@pytest.fixture
def password_set(monkeypatch):
    """Configure a known password and yield its bcrypt hash."""
    password_hash = hash_password("correct-horse-battery")
    monkeypatch.setattr(settings.security, "password_hash", password_hash)
    return password_hash


@pytest.fixture
def project_gate():
    """Return an installer that records a route module's project validation.

    ``install(monkeypatch, route_module, calls)`` replaces the module's
    ``project_service`` with one whose ``get_project_path`` appends
    ``("project", project_id)`` to *calls*, so a test can assert that the
    project check runs before the delegated service call.
    """

    def install(monkeypatch, route_module, calls):
        monkeypatch.setattr(
            route_module,
            "project_service",
            SimpleNamespace(
                get_project_path=lambda project_id: calls.append(("project", project_id)),
            ),
        )

    return install
