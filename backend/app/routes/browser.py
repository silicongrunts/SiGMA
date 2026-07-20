"""Browser API routes - Chrome + noVNC management for Explore tab."""
from fastapi import APIRouter

from app.services.browser_service import get_browser_service
from app.services.project_service import project_service
from app.core.response import ok

router = APIRouter(prefix="/browser", tags=["browser"])


@router.get("/{project_id}/status")
async def get_browser_status(project_id: str):
    project_service.get_project_path(project_id)
    service = get_browser_service()
    data = await service.get_status()
    return ok(data)


@router.post("/{project_id}/start")
async def start_browser(project_id: str):
    project_service.get_project_path(project_id)
    service = get_browser_service()
    result = await service.start()
    return ok(result)


@router.post("/{project_id}/stop")
async def stop_browser(project_id: str):
    project_service.get_project_path(project_id)
    service = get_browser_service()
    await service.stop()
    return ok({"status": "stopped"})


@router.post("/{project_id}/clear-data")
async def clear_browser_data(project_id: str):
    """Stop the browser, wipe its profile dir, and restart it cleanly.

    Recovers from a corrupted or version-incompatible profile without a
    full container restart.
    """
    project_service.get_project_path(project_id)
    service = get_browser_service()
    result = await service.clear_data()
    return ok(result)
