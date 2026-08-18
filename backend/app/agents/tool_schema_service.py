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
    """Append a model-context clause to the read tool's base prompt.

    The base prompt (PROMPT_READ, set on the ToolDefinition) stays the single
    source of truth for behavior; only the image-capability difference between
    model contexts is added here.
    """
    next_schema = deepcopy(schema)
    if accepts_images:
        variant = (
            "In this model context, Read also supports PNG and JPG image files "
            "up to 3840x3840 pixels; image contents are injected directly for "
            "visual inspection."
        )
    else:
        variant = (
            "In this model context, Read cannot inject image bytes directly; "
            "it returns the image path instead — use vision_analyze with that "
            "path when visual inspection is needed."
        )
    function = next_schema.get("function", {})
    function["description"] = (
        f"{function.get('description', '')}\n\n{variant}"
    ).strip()
    return next_schema


def _browser_vision_direct_schema(schema: dict) -> dict:
    next_schema = deepcopy(schema)
    function = next_schema.get("function", {})
    function["description"] = (
        "Take a viewport screenshot and return it for direct visual inspection "
        "in this model context. Use when the page snapshot misses layout, "
        "charts, colors, or visual state. question is required by the schema "
        "but is not used in this mode. tab_id selects the tab (default: the "
        "active tab); element_ref optionally crops to an element from the "
        "latest snapshot."
    )
    return next_schema
