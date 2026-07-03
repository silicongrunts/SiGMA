"""
REST endpoints for terminal session discovery.

Terminal sessions are normally acquired via WebSocket (see
``app.core.terminal_ws``), but the client needs a way to discover
existing sessions after a page refresh or browser switch where
localStorage may be empty.
"""

from fastapi import APIRouter

from app.core.response import ok
from app.services.terminal_service import terminal_service

router = APIRouter(prefix="/terminal", tags=["terminal"])


@router.get("/{project_id}/sessions")
async def list_sessions(project_id: str):
    """Return all ACTIVE / ORPHANED terminal sessions for *project_id*.

    Each entry contains ``session_id``, ``slot``, and ``state``.
    Sorted by slot number.
    """
    sessions = terminal_service.list_project_sessions(project_id)
    return ok({"sessions": sessions})
