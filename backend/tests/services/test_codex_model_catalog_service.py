"""Tests for the Codex model catalog service.

Covers source ordering (GitHub first, mirror fallback), strict catalog
validation, visibility filtering, userdata cache persistence, and the
total-failure path that must leave the previous cache untouched.
"""

import json

import httpx
import pytest

import app.services.codex_model_catalog_service as catalog_module
from app.core.exceptions import CodexCatalogError
from app.services.codex_model_catalog_service import CodexModelCatalogService

GITHUB_SOURCE = catalog_module.CATALOG_SOURCES[0]
MIRROR_SOURCE = catalog_module.CATALOG_SOURCES[1]


def _catalog(*models):
    return {
        "models": [
            {"slug": slug, "visibility": visibility}
            for slug, visibility in models
        ]
    }


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None
            )

    def json(self):
        return self._payload


class _FakeClient:
    """httpx.AsyncClient stand-in answering per registered source handlers."""

    handlers = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        handler = type(self).handlers.get(url)
        if handler is None:
            raise httpx.ConnectError(f"unregistered source {url}")
        result = handler()
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    path = tmp_path / "codex_model_catalog.json"
    monkeypatch.setattr(catalog_module, "CATALOG_CACHE_FILE", path)
    return path


@pytest.fixture
def fake_http(monkeypatch):
    def install(handlers):
        _FakeClient.handlers = handlers
        monkeypatch.setattr(catalog_module.httpx, "AsyncClient", _FakeClient)
    return install


@pytest.mark.unit
async def test_refresh_prefers_github_and_persists_cache(service, cache_file, fake_http):
    fake_http({
        GITHUB_SOURCE: lambda: _FakeResponse(_catalog(("gpt-5.5", "list"), ("gpt-5.6-sol", "list"))),
    })

    data = await service.refresh()

    assert data["models"] == ["gpt-5.5", "gpt-5.6-sol"]
    assert data["source"] == GITHUB_SOURCE
    assert data["fetched_at"] > 0
    stored = json.loads(cache_file.read_text())
    assert stored["models"] == ["gpt-5.5", "gpt-5.6-sol"]
    assert service.cached_models()["models"] == ["gpt-5.5", "gpt-5.6-sol"]


@pytest.mark.unit
async def test_refresh_falls_back_to_mirror(service, cache_file, fake_http):
    fake_http({
        GITHUB_SOURCE: lambda: httpx.ConnectError("unreachable"),
        MIRROR_SOURCE: lambda: _FakeResponse(_catalog(("gpt-5.5", "list"))),
    })

    data = await service.refresh()

    assert data["source"] == MIRROR_SOURCE
    assert data["models"] == ["gpt-5.5"]


@pytest.mark.unit
async def test_refresh_filters_hidden_models_and_deduplicates(service, cache_file, fake_http):
    fake_http({
        GITHUB_SOURCE: lambda: _FakeResponse(_catalog(
            ("gpt-5.5", "list"),
            ("gpt-secret", "hide"),
            ("gpt-5.5", "list"),
        )),
    })

    data = await service.refresh()

    assert data["models"] == ["gpt-5.5"]


@pytest.mark.unit
async def test_malformed_catalog_rejects_source_and_tries_next(service, cache_file, fake_http):
    fake_http({
        GITHUB_SOURCE: lambda: _FakeResponse({"models": [{"display_name": "no slug"}]}),
        MIRROR_SOURCE: lambda: _FakeResponse(_catalog(("gpt-5.5", "list"))),
    })

    data = await service.refresh()

    assert data["source"] == MIRROR_SOURCE


@pytest.mark.unit
async def test_total_failure_raises_and_keeps_previous_cache(service, cache_file, fake_http):
    cache_file.write_text(json.dumps({
        "models": ["gpt-old"], "source": "old", "fetched_at": 1.0,
    }))
    fake_http({
        GITHUB_SOURCE: lambda: httpx.ConnectError("unreachable"),
        MIRROR_SOURCE: lambda: _FakeResponse({"unexpected": True}, status_code=500),
    })

    with pytest.raises(CodexCatalogError, match="any source"):
        await service.refresh()

    assert service.cached_models()["models"] == ["gpt-old"]


@pytest.mark.unit
def test_cached_models_tolerates_missing_or_corrupt_cache(service, cache_file):
    assert service.cached_models() == {"models": [], "source": None, "fetched_at": None}

    cache_file.write_text("not json")
    assert service.cached_models()["models"] == []

    cache_file.write_text(json.dumps({"models": ["ok", 7, None], "source": "s", "fetched_at": 2.0}))
    assert service.cached_models()["models"] == ["ok"]


@pytest.fixture
def service():
    return CodexModelCatalogService()
