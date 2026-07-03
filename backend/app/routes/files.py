import mimetypes

from fastapi import APIRouter, UploadFile, Form, Query, Response
from fastapi.responses import FileResponse

from app.models.requests import FileCreate, FileContent, FileRename, FileMove, FileExtractRequest, FileBatchDownloadRequest
from app.services.file_service import file_service
from app.core.downloads import download_headers
from app.core.response import ok

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/{project_id}/tree")
async def get_tree(project_id: str):
    data = await file_service.get_project_tree(project_id)
    return ok(data)


@router.get("/{project_id}/children")
async def get_children(project_id: str, path: str = Query("")):
    """Return only immediate children of a directory (one level, non-recursive)."""
    data = await file_service.get_children(project_id, path)
    return ok(data)


@router.get("/{project_id}/content")
async def get_content(project_id: str, path: str):
    """Return raw file content as text/plain with content hash header."""
    content = await file_service.read_file(project_id, path)
    content_hash = file_service.compute_hash(content)
    return Response(content=content, media_type="text/plain", headers={"X-Content-Hash": content_hash})


@router.get("/{project_id}/inline")
async def inline_file(project_id: str, path: str = Query("")):
    """Serve a project file inline, for sanitized markdown images."""
    full_path = await file_service.get_project_file_path(project_id, path)
    media_type = mimetypes.guess_type(full_path.name)[0] or "application/octet-stream"
    return FileResponse(path=str(full_path), media_type=media_type)


@router.post("/{project_id}/content")
async def update_content(project_id: str, data: FileContent):
    result = await file_service.write_file(
        project_id, data.path, data.content,
        force=data.force, expected_hash=data.hash,
        require_expected_hash=True,
    )
    return ok(result)


@router.post("/{project_id}/create")
async def create_item(project_id: str, data: FileCreate):
    await file_service.create_item(project_id, data.path, data.type == "directory")
    return ok(None)


@router.post("/{project_id}/move")
async def move_item(project_id: str, data: FileMove):
    await file_service.move_item(project_id, data.source, data.destination)
    return ok(None)


@router.post("/{project_id}/rename")
async def rename_item(project_id: str, data: FileRename):
    await file_service.rename_item(project_id, data.path, data.new_name)
    return ok(None)


@router.delete("/{project_id}")
async def delete_item(project_id: str, path: str):
    await file_service.delete_item(project_id, path)
    return ok(None)


@router.get("/{project_id}/download")
async def download_item(project_id: str, path: str = Query("")):
    """Download a file or directory as ZIP (binary, exempt from unified format)."""
    info = await file_service.get_download_info(project_id, path)
    if info["is_file"]:
        return FileResponse(
            path=str(info["full_path"]), filename=info["name"],
            media_type="application/octet-stream",
        )
    else:
        zip_data = await file_service.create_zip(project_id, path)
        filename = info["name"] if path else "project"
        return Response(
            content=zip_data, media_type="application/zip",
            headers=download_headers(f"{filename}.zip"),
        )


@router.post("/{project_id}/upload")
async def upload_files(project_id: str, file: UploadFile, path: str = Form(""), overwrite: bool = Form(False)):
    content = await file.read()
    filename = await file_service.save_upload(project_id, file.filename, content, path, overwrite=overwrite)
    return ok({"filename": filename})


@router.post("/{project_id}/extract")
async def extract_archive(project_id: str, data: FileExtractRequest):
    """Extract a zip/tar archive. Returns conflicts if overwrite=False and conflicts exist."""
    if not data.overwrite and not data.skip_conflicts:
        conflicts = await file_service.check_extract_conflicts(project_id, data.path)
        if conflicts:
            return ok({"conflicts": conflicts})
    result = await file_service.extract_archive(project_id, data.path, overwrite=data.overwrite)
    return ok(result)


@router.post("/{project_id}/batch-download")
async def batch_download(project_id: str, data: FileBatchDownloadRequest):
    """Download multiple files/directories as a single ZIP."""
    zip_data = await file_service.create_multi_zip(project_id, data.paths)
    return Response(
        content=zip_data,
        media_type="application/zip",
        headers=download_headers("selected.zip"),
    )
