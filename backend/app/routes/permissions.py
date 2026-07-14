"""
Permission routes — auto-approve settings for agent write operations.

Permission approval no longer uses a dedicated HTTP endpoint. When the LLM
agent needs user approval for a write/bash/notebook operation, the task is
parked as ``awaiting_input`` (same mechanism as interactive tools like
``ask_user_question``). The user's response flows back through the chat resume
path (``POST /chat/stream`` with ``resume=true`` and
``interaction_response``). This makes the permission flow crash-safe: a
worker restart or page refresh does not lose the pending request.

This module exposes the per-project auto-approve settings (one toggle per
permission category) persisted in ``project_config``.
"""

from fastapi import APIRouter

from app.models.requests import AutoApproveUpdate
from app.core.response import ok
from app.core.exceptions import ServiceException
from app.services.permission_executor import PERMISSION_CATEGORIES
from app.services.project_service import project_service

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.get("/{project_id}/auto-approve")
async def get_auto_approve(project_id: str):
    """Return the four-category auto-approve flags for a project."""
    project_service.get_project_path(project_id)
    settings = await project_service.get_auto_approve(project_id)
    return ok(settings)


@router.put("/{project_id}/auto-approve")
async def set_auto_approve(project_id: str, data: AutoApproveUpdate):
    """Toggle one auto-approve category for a project."""
    if data.category not in PERMISSION_CATEGORIES:
        raise ServiceException(
            f"Invalid permission category: {data.category}",
            code="PERMISSION_INVALID_CATEGORY",
            status_code=400,
        )
    project_service.get_project_path(project_id)
    await project_service.set_auto_approve(project_id, data.category, data.enabled)
    return ok({"category": data.category, "enabled": data.enabled})
