"""Image generation tools."""

from app.agents.prompts import PROMPT_DRAW_IMAGE
from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.core.config import settings
from app.core.logging import get_logger
from app.services.draw_service import draw_service

logger = get_logger(__name__)


async def _draw_image(prompt: str, project_id: str = "") -> str:
    if not settings.DRAW_MODEL:
        return "Error: draw model is not configured."
    if not (prompt or "").strip():
        return "Error: prompt is required."

    try:
        result = await draw_service.draw_image(project_id, prompt)
    except Exception as exc:
        logger.exception("Failed to generate image")
        return f"Error: failed to generate image: {exc}"

    if result.get("error"):
        return f"Error: {result['error']}"
    path = result["path"]
    return (
        f"Generated image saved at `{path}`.\n\n"
        f"Use this Markdown to show it in chat: ![]({path})"
    )


tool_registry.register(ToolDefinition(
    name="draw_image",
    description="Generate an image from a detailed prompt and save it in the project",
    input_schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Detailed visual prompt for the draw model. Include subject, "
                    "composition, style, colors, labels, and constraints."
                ),
            },
        },
        "required": ["prompt"],
    },
    call=_draw_image,
    prompt=PROMPT_DRAW_IMAGE,
    requires_project_id=True,
    is_read_only=False,
))
