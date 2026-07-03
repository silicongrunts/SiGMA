"""Dynamic tool schema selection for model-role-specific capabilities."""

from __future__ import annotations

from copy import deepcopy

from app.agents.tools.registry import tool_registry
from app.core.model_config import model_role_accepts_images


def tool_schemas_for_model_role(
    model_role: str,
    allowed_tools: frozenset[str] | None = None,
) -> list[dict]:
    """Return OpenAI tool schemas visible to one model role.

    Vision-related tools depend on whether the current model can receive images
    directly. The runtime whitelist still enforces access; this function only
    controls what the model sees in the tools parameter.
    """
    accepts_images = model_role_accepts_images(model_role)
    schemas: list[dict] = []
    for tool in tool_registry.list_all():
        if allowed_tools is not None and tool.name not in allowed_tools:
            continue
        if tool.name == "vision_analyze" and accepts_images:
            continue
        if tool.name == "read":
            schemas.append(_read_schema(tool.to_openai_schema(), accepts_images))
            continue
        if tool.name == "browser_vision" and accepts_images:
            schemas.append(_browser_vision_direct_schema(tool.to_openai_schema()))
            continue
        schemas.append(tool.to_openai_schema())
    return schemas


def _read_schema(schema: dict, accepts_images: bool) -> dict:
    next_schema = deepcopy(schema)
    function = next_schema.get("function", {})
    base = (
        "Reads text files from the local filesystem. By default, it returns up "
        "to 200 lines and supports optional offset/limit line ranges."
    )
    if accepts_images:
        function["description"] = (
            f"{base} In this model context, Read also supports PNG and JPG "
            "image files up to 3840x3840 pixels; image contents are injected "
            "directly for visual inspection."
        )
    else:
        function["description"] = (
            f"{base} In this model context, Read can identify PNG and JPG image "
            "files but cannot inject image bytes directly; use vision_analyze "
            "with the returned image path when visual inspection is needed. "
            "Other binary files will return a binary-file error."
        )
    return next_schema


def _browser_vision_direct_schema(schema: dict) -> dict:
    next_schema = deepcopy(schema)
    function = next_schema.get("function", {})
    function["description"] = (
        "Take a browser screenshot and return it for direct visual inspection. "
        "Use when the page snapshot misses layout, charts, colors, or visual state."
    )
    return next_schema
