"""
Tests for the /system routes.

The litellm metadata and YAML rendering tests exercise the route handlers
directly; the /system/settings tests drive the real app through the shared
``client`` fixture so auth and error handling run end-to-end. Persistence is
stubbed or redirected to ``tmp_path``, so the real settings.yaml is never
touched.
"""

import pytest

from app.core.auth import SESSION_COOKIE_NAME, session_token
from app.core.config import settings, settings_to_dict
from app.routes import system


@pytest.mark.asyncio
async def test_litellm_provider_and_static_model_metadata():
    """The routes surface the installed litellm's provider/model data. The
    expected values are derived from the installed litellm itself so a
    litellm version bump cannot break the tests; the contract under test is
    SiGMA's wiring, not litellm's data."""
    providers = await system.list_litellm_providers()
    assert providers["data"]["providers"]  # non-empty provider list

    models = await system.list_litellm_models(system.ModelListRequest(provider="openrouter"))
    assert any(model.startswith("openrouter/") for model in models["data"]["models"])


@pytest.mark.asyncio
async def test_litellm_context_metadata():
    """get_litellm_context resolves the model key and extracts the context
    length (max_input_tokens, falling back to max_tokens) from litellm's
    own metadata, compared here against litellm directly."""
    import litellm

    model_key, expected = next(
        (key, info[field])
        for key, info in litellm.model_cost.items()
        if isinstance(info, dict) and "/" in key
        for field in ("max_input_tokens", "max_tokens")
        if isinstance(info.get(field), int) and info[field] > 0
    )

    response = await system.get_litellm_context(model=model_key, provider="")

    assert response["data"]["model"] == model_key
    assert response["data"]["max_context_length"] == expected


@pytest.mark.asyncio
async def test_render_settings_yaml_from_structured_config():
    response = await system.render_settings_yaml(
        system.SettingsDataUpdate(config=settings_to_dict(settings))
    )

    assert response["data"]["content"].startswith("app:\n")


# ---------------------------------------------------------------------------
# GET/PUT /system/settings — password-hash handling contract.
#
# The hash is server-managed: GET returns it as-is and PUT re-injects the
# persisted value, so the browser can never wipe the password through a
# settings save. Persistence goes through stubs; no real file is touched.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_settings_returns_hash(client, password_set, monkeypatch, tmp_path):
    """The real bcrypt hash is returned (not redacted). The frontend derives
    password_enabled from its non-emptiness. The YAML body is read from the
    (temporary) settings file the route points at."""
    settings_file = tmp_path / "settings.yaml"
    settings_file.write_text("app:\n  api_prefix: /api/v1\n", encoding="utf-8")
    monkeypatch.setattr(system, "SETTINGS_FILE", settings_file)

    client.cookies.set(SESSION_COOKIE_NAME, session_token(password_set))
    r = await client.get("/api/v1/system/settings")

    assert r.status_code == 200
    body = r.json()
    assert body["data"]["path"] == str(settings_file)
    assert body["data"]["config"]["security"]["password_hash"] == password_set


@pytest.mark.asyncio
async def test_full_config_put_preserves_hash(client, password_set, monkeypatch):
    captured = {}

    def fake_save_settings_data(data):
        captured["data"] = data
        # Simulate the persisted hash being retained.
        return settings

    monkeypatch.setattr("app.routes.system.save_settings_data", fake_save_settings_data)
    client.cookies.set(SESSION_COOKIE_NAME, session_token(password_set))
    # Client submits a config that OMITS the security block entirely.
    payload = {"config": {"app": {"api_prefix": "/api/v1"}, "models": {}}}
    r = await client.put("/api/v1/system/settings", json=payload)

    assert r.status_code == 200
    # The hash must have been re-injected before saving.
    assert captured["data"]["security"]["password_hash"] == password_set


@pytest.mark.asyncio
async def test_put_ignores_client_security_block(client, password_set, monkeypatch):
    """PUT /system/settings must always ignore any client-supplied security
    block and re-inject the server's persisted hash. A stray forbidden key
    (e.g. password_enabled) must be dropped, not cause a 422."""
    captured = {}

    def fake_save_settings_data(data):
        captured["data"] = data
        return settings

    monkeypatch.setattr("app.routes.system.save_settings_data", fake_save_settings_data)
    client.cookies.set(SESSION_COOKIE_NAME, session_token(password_set))
    # A malformed security block including a forbidden password_enabled key,
    # plus a bogus hash that must never be persisted.
    payload = {
        "config": {
            "app": {"api_prefix": "/api/v1"},
            "models": {},
            "security": {"password_enabled": True, "password_hash": "bogus"},
        }
    }
    r = await client.put("/api/v1/system/settings", json=payload)

    assert r.status_code == 200, r.text
    # The server's persisted hash wins; the bogus value and forbidden key are gone.
    assert "password_enabled" not in captured["data"]["security"]
    assert captured["data"]["security"]["password_hash"] == password_set


@pytest.mark.asyncio
async def test_check_endpoint_tolerates_security_block(client, password_set):
    """The Provider-test flow (POST /settings/check) must not fail with
    extra_forbidden when the config includes a security block."""
    client.cookies.set(SESSION_COOKIE_NAME, session_token(password_set))
    payload = {
        "config": {
            "app": {"api_prefix": "/api/v1"},
            "security": {"password_enabled": True},
        }
    }
    r = await client.post("/api/v1/system/settings/check", json=payload)

    assert r.status_code == 200
    # The regression is specifically the extra_forbidden / password_enabled
    # validation error. The structure check may still fail for other reasons
    # (here: missing supervisor model), but it must NOT mention the forbidden
    # key or the security block.
    body = r.content.decode("utf-8")
    assert "password_enabled" not in body
    assert "Extra inputs are not permitted" not in body
    assert "extra_forbidden" not in body
