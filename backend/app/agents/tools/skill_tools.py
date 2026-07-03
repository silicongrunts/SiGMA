"""
Skill tools — ``skill_load`` for loading skill content.

Follows the same registration pattern as bash.py and other tool modules.
"""

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.prompts import PROMPT_SKILL_LOAD
from app.core.logging import get_logger
from app.services.skill_service import skill_service

logger = get_logger(__name__)


async def _skill_load(skill_id: str, file_path: str | None = None) -> str:
    """Load content from a skill directory.

    Args:
        skill_id: Folder name of the skill.
        file_path: Optional relative path inside the skill dir (defaults to SKILL.md).

    Returns:
        File content as UTF-8 text, or an error message.
    """
    try:
        return skill_service.get_skill_content(skill_id, file_path)
    except Exception as exc:
        logger.exception("Failed to load skill %s", skill_id)
        return f"[skill_load error] {exc}"


# ── Register ──

tool_registry.register(ToolDefinition(
    name="skill_load",
    description="Load the full content of a skill by its folder ID. Returns SKILL.md or a specified file inside the skill directory.",
    prompt=PROMPT_SKILL_LOAD,
    input_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Skill folder name (the <id> value from the <skills> list)",
            },
            "file_path": {
                "type": "string",
                "description": "Relative path inside skill only; no absolute path. Defaults to SKILL.md.",
            },
        },
        "required": ["id"],
    },
    call=lambda id, file_path=None, **_kw: _skill_load(id, file_path),
    requires_project_id=False,
    is_read_only=True,
))
