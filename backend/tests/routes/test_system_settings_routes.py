import pytest

from app.core.config import settings, settings_to_dict
from app.routes import system


@pytest.mark.asyncio
async def test_litellm_provider_and_static_model_metadata():
    providers = await system.list_litellm_providers()
    assert "openai" in providers["data"]["providers"]

    models = await system.list_litellm_models(system.ModelListRequest(provider="openrouter"))
    assert any(model.startswith("openrouter/") for model in models["data"]["models"])


@pytest.mark.asyncio
async def test_litellm_context_metadata():
    response = await system.get_litellm_context(
        model="openrouter/anthropic/claude-opus-4.5",
        provider="",
    )

    assert response["data"]["max_context_length"] == 200000


@pytest.mark.asyncio
async def test_render_settings_yaml_from_structured_config():
    response = await system.render_settings_yaml(
        system.SettingsDataUpdate(config=settings_to_dict(settings))
    )

    assert response["data"]["content"].startswith("app:\n")
