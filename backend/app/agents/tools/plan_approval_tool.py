"""
Plan approval tool — submit_plan_for_approval.

Two-phase interactive tool for the plan agent:
- Phase 1: returns interaction data, loop pauses for user approval
- Phase 2: user approves → save plan file; user rejects → return feedback
"""

import re
from pathlib import Path

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.core.config import settings
from app.core.logging import get_logger
from app.core.utils import generate_id, utcnow
from app.services.session_temp_service import session_temp_service

logger = get_logger(__name__)


def _sanitize_plan_content(content: str) -> str:
    """Remove control characters except newlines/tabs."""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', content)


async def _save_plan(project_id: str, session_id: str, plan_content: str) -> str:
    """Save *plan_content* to the main session temporary storage.

    File naming: ``YYYYMMDD-HHMMSS-{short_id}.md`` (UTC).
    """
    if not plan_content or not plan_content.strip():
        raise ValueError("Plan content cannot be empty")
    if not session_id:
        raise ValueError("Session ID is required for saving plans")

    project_path = Path(settings.get_project_path(project_id)).resolve()
    plans_dir = session_temp_service.ensure_child_dir(project_id, session_id, "plans")

    timestamp = utcnow().strftime("%Y%m%d-%H%M%S")
    short_id = generate_id()[:6]
    filename = f"{timestamp}-{short_id}.md"

    plan_path = plans_dir / filename
    try:
        plan_path.resolve().relative_to(project_path.resolve())
    except ValueError:
        raise ValueError("Plan path escaped project directory")

    clean_content = _sanitize_plan_content(plan_content)
    tmp_path = plan_path.with_name(f".{filename}.tmp")
    tmp_path.write_text(clean_content, encoding="utf-8")
    tmp_path.rename(plan_path)

    relative_path = str(plan_path.relative_to(project_path))
    logger.info("Plan saved: %s", relative_path)
    return relative_path


async def _submit_plan_for_approval(plan_content: str = "", **_kwargs) -> dict:
    """Phase 1: Return interaction data for frontend approval dialog."""
    return {
        "interaction_type": "submit_plan_for_approval",
        "plan_content": plan_content,
    }


async def _submit_plan_for_approval_phase2(
    plan_content: str = "",
    approved: bool | None = None,
    feedback: str = "",
    project_id: str = "",
    session_id: str = "",
    **_kwargs,
) -> str:
    """Phase 2: Handle user approval or rejection."""
    if not approved:
        fb = feedback.strip() if feedback else "No specific feedback provided"
        return (
            f"Plan rejected. Feedback: {fb}\n"
            "Please revise the plan based on this feedback and submit again."
        )

    # User approved: save the plan.
    if not project_id:
        return "Error: Could not determine project for saving plan."
    if not session_id:
        return "Error: Could not determine session temporary storage for saving plan."

    try:
        await _save_plan(project_id, session_id, plan_content)
        return (
            "The plan was approved and saved to internal session temporary "
            "storage. Here's the content of this plan:"
        )
    except Exception as e:
        logger.exception("Failed to save approved plan")
        return f"Error saving plan: {e}. Please try again."


async def _submit_plan_dispatch(
    plan_content: str = "",
    approved: bool | None = None,
    feedback: str = "",
    project_id: str = "",
    session_id: str = "",
    **_kwargs,
) -> str | dict:
    """Dispatch between phase 1 and phase 2."""
    if approved is not None:
        # Phase 2: user responded
        result = await _submit_plan_for_approval_phase2(
            plan_content=plan_content, approved=approved, feedback=feedback,
            project_id=project_id, session_id=session_id,
        )
        return result
    # Phase 1: first call
    return await _submit_plan_for_approval(plan_content=plan_content)


tool_registry.register(ToolDefinition(
    name="submit_plan_for_approval",
    description="Submit an implementation plan for user approval",
    input_schema={
        "type": "object",
        "properties": {
            "plan_content": {
                "type": "string",
                "description": "Complete implementation plan in Markdown format",
            },
        },
        "required": ["plan_content"],
    },
    call=_submit_plan_dispatch,
    prompt=(
        "Submit a complete implementation plan for user approval. "
        "The plan must be in Markdown format. After submission, the user will "
        "review and either approve or reject with feedback. If rejected, revise "
        "the plan and submit again."
    ),
    requires_user_interaction=True,
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=True,
))
