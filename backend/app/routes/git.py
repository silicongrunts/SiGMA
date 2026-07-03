from fastapi import APIRouter, Query
from typing import Optional

from app.services.git_service import git_service
from app.core.downloads import download_headers
from app.core.response import ok

router = APIRouter(prefix="/git", tags=["git"])


@router.post("/{project_id}/init")
async def init_git(project_id: str):
    success = git_service.init_git(project_id)
    return ok({"initialized": success})


@router.get("/{project_id}/log")
async def get_log(
    project_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    before: Optional[str] = Query(None),
):
    commits = git_service.get_log(project_id, limit, offset, before)
    return ok({"commits": commits})


@router.get("/{project_id}/diff")
async def get_diff(
    project_id: str,
    path: str = Query(...),
    commit: Optional[str] = Query(None),
    parent_commit: Optional[str] = Query(None),
    short_hash: Optional[str] = Query(None),
):
    diff_data = git_service.get_diff_with_defaults(
        project_id, path, commit, short_hash, parent_commit,
    )
    return ok(diff_data)


@router.get("/{project_id}/blob")
async def get_blob(
    project_id: str,
    path: str = Query(...),
    commit: str = Query(...),
):
    result = git_service.get_blob(project_id, path, commit)
    return ok(result)


@router.get("/{project_id}/commit-files")
async def get_commit_files(
    project_id: str,
    commit: str = Query(...),
    parent_commit: Optional[str] = Query(None),
):
    files = git_service.get_commit_files(project_id, commit, parent_commit)
    return ok({"files": files})


@router.get("/{project_id}/history")
async def get_file_history(project_id: str, path: str = Query(...)):
    history = git_service.get_file_history(project_id, path)
    return ok({"history": history})


@router.get("/{project_id}/snapshot")
async def get_snapshot(
    project_id: str,
    commit: str = Query(...),
):
    """Download a commit snapshot as ZIP (binary download, exempt from unified format)."""
    from fastapi.responses import Response
    zip_bytes = git_service.get_snapshot_zip(project_id, commit)
    return Response(
        content=zip_bytes, media_type="application/zip",
        headers=download_headers(f"{project_id}_{commit[:7]}.zip"),
    )
