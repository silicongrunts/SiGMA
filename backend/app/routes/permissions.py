"""
Permission routes — user approval for agent write operations outside sandbox.

When the LLM agent (via QueryLoop) tries to write a file outside the project
directory or /tmp, the frontend shows a permission dialog.  The user's
response is forwarded here, which relays it back to the worker via
StreamServer.
"""

from fastapi import APIRouter

from app.models.requests import PermissionRespondRequest
from app.core.response import ok
from app.core.exceptions import ServiceException
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
