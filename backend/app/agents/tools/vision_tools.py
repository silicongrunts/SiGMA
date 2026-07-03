"""General image-inspection tool backed by the configured vision model."""

from __future__ import annotations

from app.agents.prompts import PROMPT_VISION_ANALYZE
from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.core.config import settings
from app.core.logging import get_logger
from app.services.chat_attachments import read_image_path_base64
from app.services.llm_service import llm_service

logger = get_logger(__name__)


async def _vision(question: str, image_path: str, project_id: str) -> str:
    if not settings.VISION_MODEL:
        return "Error: vision model is not configured."
    if not question.strip():
        return "Error: question is required."
    if not image_path.strip():
        return "Error: image_path is required."

    try:
        image_base64, media_type = await read_image_path_base64(project_id, image_path)
        analysis = await llm_service.call_vision(
            prompt=question,
            image_base64=image_base64,
            image_media_type=media_type,
        )
    except Exception as exc:
        logger.exception("Failed to analyze image with vision model")
        return f"Error: vision analysis failed: {exc}"

    return analysis or "(vision model returned no analysis)"


tool_registry.register(ToolDefinition(
    name="vision_analyze",
    description="Analyze an image path with the configured vision model.",
    prompt=PROMPT_VISION_ANALYZE,
    input_schema={
        "type": "object",
        "properties": {
            "image_path": {
                "type": "string",
                "description": "Absolute or project-relative image path.",
            },
            "question": {
                "type": "string",
                "description": "What to inspect or answer about the image.",
            },
        },
        "required": ["image_path", "question"],
    },
    call=_vision,
    requires_project_id=True,
    is_read_only=True,
))
