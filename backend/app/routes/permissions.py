"""
Permission routes — user approval for agent write operations outside sandbox.

When the LLM agent (via QueryLoop) tries to write a file outside the project
directory or /tmp, the frontend shows a permission dialog.  The user's
response is forwarded here, which relays it back to the worker via
StreamServer.

Also exposes the per-project auto-approve settings (one toggle per permission
category) persisted in ``project_config``.
"""

from fastapi import APIRouter

from app.models.requests import AutoApproveUpdate, PermissionRespondRequest
from app.core.response import ok
from app.core.exceptions import ServiceException
from app.services.permission_executor import PERMISSION_CATEGORIES
from app.services.project_service import project_service
from app.workers.stream_server import stream_server

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.post("/{project_id}/{task_id}/respond")
async def respond_permission(project_id: str, task_id: str, data: PermissionRespondRequest):
    """Submit user approval/denial for a pending permission request."""
    project_service.get_project_path(project_id)
    sent = await stream_server.respond_permission(
        task_id=task_id,
        request_id=data.request_id,
        approved=data.approved,
        reason=data.reason or "",
    )
    if not sent:
        raise ServiceException(
            "No active task found for this permission request",
            code="PERMISSION_TASK_NOT_FOUND",
            status_code=404,
        )
    return ok({"request_id": data.request_id, "approved": data.approved})


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
