from app.core.config import settings
from app.core.config import ModelSettings
from app.core.model_config import get_model_endpoint, model_role_accepts_images
from app.agents.tool_schema_service import tool_schemas_for_model_role
from app.agents import tools as _tools  # noqa: F401 - load tool registrations


def test_role_endpoint_uses_explicit_role_config(monkeypatch):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="claude-opus",
        provider="anthropic",
        api_key="sk-supervisor",
    ))

    endpoint = get_model_endpoint("supervisor")

    assert endpoint.litellm_model == "anthropic/claude-opus"
    assert endpoint.litellm_kwargs() == {"api_key": "sk-supervisor"}


def test_custom_base_url_uses_explicit_openai_model_prefix(monkeypatch):
    monkeypatch.setattr(settings.models, "ra", ModelSettings(
        model="openai/deepseek-chat",
        api_key="sk-ra",
        base_url="https://llm-gateway.example.com/v1/",
        extra={"custom_llm_provider": "openai"},
    ))

    endpoint = get_model_endpoint("ra")

    assert endpoint.litellm_model == "openai/deepseek-chat"
    assert endpoint.litellm_kwargs() == {
        "api_key": "sk-ra",
        "api_base": "https://llm-gateway.example.com/v1",
        "custom_llm_provider": "openai",
    }


def test_embedding_with_only_model_is_local(monkeypatch):
    monkeypatch.setattr(settings.models, "embedding", ModelSettings(model="local-embedding"))

    endpoint = get_model_endpoint("embedding")

    assert endpoint.is_local
    assert endpoint.model == "local-embedding"


def test_rerank_with_provider_is_cloud(monkeypatch):
    monkeypatch.setattr(settings.models, "rerank", ModelSettings(
        model="rerank-v3.5",
        provider="cohere",
        api_key="sk-rerank",
    ))

    endpoint = get_model_endpoint("rerank")

    assert not endpoint.is_local
    assert endpoint.litellm_model == "cohere/rerank-v3.5"


def test_provider_is_preserved_for_nested_model_names(monkeypatch):
    monkeypatch.setattr(settings.models, "draw", ModelSettings(
        model="google/gemini-2.5-flash-image-preview",
        provider="openrouter",
    ))

    endpoint = get_model_endpoint("draw")

    assert endpoint.litellm_model == "openrouter/google/gemini-2.5-flash-image-preview"


def test_ra_can_reuse_supervisor_endpoint(monkeypatch):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(
        model="claude-sonnet",
        provider="anthropic",
        api_key="sk-supervisor",
        max_context_length=180000,
        compress_threshold=140000,
    ))
    monkeypatch.setattr(settings.models, "ra", ModelSettings(reuse="supervisor"))

    endpoint = get_model_endpoint("ra")

    assert endpoint.litellm_model == "anthropic/claude-sonnet"
    assert endpoint.litellm_kwargs() == {"api_key": "sk-supervisor"}
    assert settings.max_context_length_for_role("ra") == 180000
    assert settings.compact_threshold_for_role("ra") == 140000


def test_vision_can_reuse_ra_endpoint(monkeypatch):
    monkeypatch.setattr(settings.models, "ra", ModelSettings(
        model="gpt-4o-mini",
        provider="openai",
        api_key="sk-ra",
    ))
    monkeypatch.setattr(settings.models, "vision", ModelSettings(reuse="ra"))

    endpoint = get_model_endpoint("vision")

    assert endpoint.litellm_model == "openai/gpt-4o-mini"
    assert endpoint.litellm_kwargs() == {"api_key": "sk-ra"}


def test_model_role_accepts_images_from_vision_reuse_chain(monkeypatch):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(model="supervisor-model"))
    monkeypatch.setattr(settings.models, "ra", ModelSettings(reuse="supervisor"))
    monkeypatch.setattr(settings.models, "vision", ModelSettings(reuse="ra"))

    assert model_role_accepts_images("vision")
    assert model_role_accepts_images("ra")
    assert model_role_accepts_images("supervisor")

    monkeypatch.setattr(settings.models, "vision", ModelSettings(model="vision-model"))

    assert model_role_accepts_images("vision")
    assert not model_role_accepts_images("ra")
    assert not model_role_accepts_images("supervisor")


def test_tool_schemas_hide_vision_when_current_role_accepts_images(monkeypatch):
    monkeypatch.setattr(settings.models, "vision", ModelSettings(reuse="supervisor"))

    schemas = tool_schemas_for_model_role("supervisor")
    names = {schema["function"]["name"] for schema in schemas}
    browser_schema = next(schema for schema in schemas if schema["function"]["name"] == "browser_vision")

    assert "vision_analyze" not in names
    assert "prompt" not in browser_schema["function"]["parameters"]["properties"]

    monkeypatch.setattr(settings.models, "vision", ModelSettings(model="vision-model"))

    schemas = tool_schemas_for_model_role("supervisor")
    names = {schema["function"]["name"] for schema in schemas}
    browser_schema = next(schema for schema in schemas if schema["function"]["name"] == "browser_vision")

    assert "vision_analyze" in names
    assert "question" in browser_schema["function"]["parameters"]["properties"]


def test_read_schema_describes_image_support_only_for_multimodal_role(monkeypatch):
    from app.agents.prompts import PROMPT_READ

    assert "can read images" not in PROMPT_READ.lower()
    assert "contents are presented visually" not in PROMPT_READ.lower()

    monkeypatch.setattr(settings.models, "vision", ModelSettings(reuse="supervisor"))
    schemas = tool_schemas_for_model_role("supervisor")
    read_schema = next(schema for schema in schemas if schema["function"]["name"] == "read")
    read_desc = read_schema["function"]["description"].lower()

    assert "supports png and jpg image files" in read_desc
    assert "injected directly" in read_desc
    assert "does not support image files" not in read_desc

    monkeypatch.setattr(settings.models, "vision", ModelSettings(model="vision-model"))
    schemas = tool_schemas_for_model_role("supervisor")
    read_schema = next(schema for schema in schemas if schema["function"]["name"] == "read")
    read_desc = read_schema["function"]["description"].lower()

    assert "cannot inject image bytes directly" in read_desc
    assert "use vision_analyze" in read_desc
    assert "injected directly" not in read_desc
