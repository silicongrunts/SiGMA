import logging
import subprocess
from typing import Any
from collections.abc import AsyncIterator
import json

import httpx
import litellm
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.core.config import (
    SETTINGS_FILE,
    Settings,
    dump_settings_yaml,
    save_settings_data,
    save_settings_yaml,
    settings,
    settings_to_dict,
    validate_settings_yaml,
)
from app.core.response import ok
from app.models.requests import (
    ModelListRequest,
    SettingsDataUpdate,
    SettingsUpdate,
    SettingsYamlUpdate,
    TeXOperationRequest,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/system", tags=["system"])

# Matched to the providers accepted by litellm.rerank() in litellm==1.81.16.
# Keep this list narrow: it drives the Rerank provider dropdown only.
RERANK_SUPPORTED_PROVIDERS = [
    "azure_ai",
    "bedrock",
    "cohere",
    "deepinfra",
    "fireworks_ai",
    "hosted_vllm",
    "huggingface",
    "infinity",
    "jina_ai",
    "litellm_proxy",
    "nvidia_nim",
    "together_ai",
    "vertex_ai",
    "voyage",
    "watsonx",
]


async def _stream_tex_operation(stream: AsyncIterator[str]):
    try:
        async for event in stream:
            yield event
    except ValueError as exc:
        yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=True)}\n\n"


@router.get("/settings")
async def get_settings_yaml():
    """Return the current settings in both structured and YAML form."""
    return ok({
        "path": str(SETTINGS_FILE),
        "content": SETTINGS_FILE.read_text(encoding="utf-8"),
        "config": settings_to_dict(settings),
    })


@router.put("/settings")
async def update_settings(data: SettingsUpdate):
    """Validate, persist, and hot-reload settings.yaml."""
    try:
        if data.content is not None:
            save_settings_yaml(data.content)
        elif data.config is not None:
            save_settings_data(data.config)
        else:
            raise ValueError("Either content or config is required")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok({
        "path": str(SETTINGS_FILE),
        "restart_required": True,
    })


@router.post("/settings/yaml")
async def render_settings_yaml(data: SettingsDataUpdate):
    """Render a structured settings draft as YAML without persisting it."""
    try:
        config = Settings.model_validate(data.config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok({"content": dump_settings_yaml(config)})


@router.post("/settings/validate-yaml")
async def validate_yaml(data: SettingsYamlUpdate):
    """Validate a YAML draft and return its structured form without saving."""
    try:
        config = validate_settings_yaml(data.content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ok({"config": settings_to_dict(config)})


@router.get("/tex/status")
async def get_tex_status():
    from app.services.tex_service import tex_service
    return ok(tex_service.get_status())


@router.post("/tex/run")
async def run_tex_operation(data: TeXOperationRequest):
    from app.services.tex_service import tex_service

    try:
        if data.operation == "set_repository":
            stream = tex_service.set_repository(data.repository or "official")
        elif data.operation == "update":
            stream = tex_service.update(data.repository)
        elif data.operation == "install_full":
            stream = tex_service.install_full(data.repository)
        elif data.operation == "install_package":
            if not data.package:
                raise ValueError("Package is required")
            stream = tex_service.install_package(data.package, data.repository)
        elif data.operation == "search":
            if not data.query:
                raise ValueError("Search query is required")
            stream = tex_service.search(data.query, data.repository)
        elif data.operation == "update_tlmgr":
            stream = tex_service.update_tlmgr()
        elif data.operation == "switch_year":
            stream = tex_service.switch_year(data.repository, data.target_year)
        else:
            raise ValueError("Unsupported TeX operation")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return StreamingResponse(_stream_tex_operation(stream), media_type="text/event-stream")


@router.get("/litellm/providers")
async def list_litellm_providers():
    providers = sorted({provider.value for provider in litellm.LlmProviders})
    return ok({
        "providers": providers,
        "provider_roles": {
            "rerank": RERANK_SUPPORTED_PROVIDERS,
        },
    })


@router.post("/settings/check")
async def check_settings(data: SettingsUpdate):
    """Check settings config structure and model connectivity without saving."""
    from app.services.settings_check_service import SettingsCheckService
    service = SettingsCheckService()
    return StreamingResponse(
        service.check(content=data.content, config=data.config),
        media_type="text/event-stream",
    )


@router.post("/litellm/models")
async def list_litellm_models(data: ModelListRequest):
    """Return model suggestions for a provider, falling back to static LiteLLM data."""
    provider = data.provider.strip()
    models = await _fetch_models_from_base_url(base_url=data.base_url, api_key=data.api_key)
    if not models:
        models = _static_models_for_provider(provider)
    return ok({"models": models[:1000]})


@router.get("/litellm/context")
async def get_litellm_context(
    model: str = "",
    provider: str = "",
):
    model_key = _litellm_model_key(model.strip(), provider.strip())
    info = litellm.model_cost.get(model_key) or litellm.model_cost.get(model.strip())
    context_length = _extract_context_length(info if isinstance(info, dict) else None)
    return ok({
        "model": model_key,
        "max_context_length": context_length,
    })


async def _fetch_models_from_base_url(*, base_url: str, api_key: str) -> list[str]:
    if not base_url:
        return []
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.debug("Failed to fetch model list from %s: %s", url, exc, exc_info=True)
        return []

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    names = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            names.append(item["id"])
        elif isinstance(item, str):
            names.append(item)
    return sorted(set(names))


def _static_models_for_provider(provider: str) -> list[str]:
    if not provider:
        return []
    names = [
        model
        for model, info in litellm.model_cost.items()
        if isinstance(info, dict) and info.get("litellm_provider") == provider
    ]
    return sorted(set(names))


def _litellm_model_key(model: str, provider: str) -> str:
    if not model or not provider:
        return model
    if model == provider or model.startswith(f"{provider}/"):
        return model
    return f"{provider}/{model}"


def _extract_context_length(info: dict[str, Any] | None) -> int | None:
    if not info:
        return None
    for key in ("max_input_tokens", "max_tokens"):
        value = info.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


# ---------------------------------------------------------------------------
# Service restart
# ---------------------------------------------------------------------------

_SUPERVISORCTL = "/usr/bin/supervisorctl"


@router.post("/restart")
async def restart_services():
    """Restart the web and worker supervisor programs.

    Spawns a detached subprocess that sleeps briefly (so the HTTP response
    can flush out), then calls supervisorctl.  ``start_new_session=True``
    puts the subprocess in its own process group — when supervisord sends
    SIGTERM to the current uvicorn the detached subprocess survives, gets
    reparented to init, and completes the restart command.
    """
    subprocess.Popen(
        ["/bin/sh", "-c", "sleep 2 && /usr/bin/supervisorctl restart web worker"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return ok({"status": "restarting"})
