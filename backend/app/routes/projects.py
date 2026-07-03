from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from app.models.requests import ProjectCreate, ProjectUpdate, ProjectConfigUpdate, ProjectRegister
from app.services.project_service import project_service
from app.services.file_service import file_service
from app.core.downloads import download_headers
from app.core.response import ok
from app.core.exceptions import FileSystemError

router = APIRouter(prefix="/projects", tags=["projects"])

# 1 GiB cap on imported zip payloads. Larger archives must be reduced before
# upload — out-of-band copies into the userdata directory are not registered
# by this endpoint and would not appear in the project list.
MAX_IMPORT_ZIP_BYTES = 1024 * 1024 * 1024


@router.get("/templates")
async def list_templates():
    return ok(project_service.list_templates())


@router.get("")
async def list_projects():
    data = await project_service.list_projects()
    return ok(data)


@router.post("", status_code=201)
async def create_project(project: ProjectCreate):
    data = await project_service.create_project(project.name, project.description, project.template)
    return ok(data)


@router.post("/import", status_code=201)
async def import_project(
    file: UploadFile = File(...),
    description: str = Form(""),
):
    """Import a project from an uploaded zip archive.

    The project name is derived from the zip filename (extension stripped).
    The archive may contain files at the root or wrap them in a single
    top-level directory (auto-stripped). See ``ProjectService.import_project``
    for full semantics on ``.SiGMA/`` / ``.git/`` preservation.
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise FileSystemError(
            "Only .zip files are accepted",
            code="INVALID_ZIP_FILENAME",
        )
    # Pre-check declared size from multipart headers so an over-limit upload
    # is rejected before the body is fully buffered in memory. The body is
    # still re-checked after read() in case the header is missing or fibbed.
    if file.size is not None and file.size > MAX_IMPORT_ZIP_BYTES:
        raise FileSystemError(
            f"Zip payload exceeds the {MAX_IMPORT_ZIP_BYTES // (1024 * 1024)} MiB limit; "
            "please reduce its size and retry",
            code="ZIP_TOO_LARGE",
        )
    name = project_service.sanitize_import_name(file.filename)
    zip_bytes = await file.read()
    if len(zip_bytes) > MAX_IMPORT_ZIP_BYTES:
        raise FileSystemError(
            f"Zip payload exceeds the {MAX_IMPORT_ZIP_BYTES // (1024 * 1024)} MiB limit; "
            "please reduce its size and retry",
            code="ZIP_TOO_LARGE",
        )
    data = await project_service.import_project(
        name, description, zip_bytes,
        max_bytes=MAX_IMPORT_ZIP_BYTES,
    )
    return ok(data)


@router.get("/unregistered")
async def list_unregistered_dirs():
    """List userdata subdirectories available for manual registration.

    Used by the fallback path when a project is too large to upload as a
    zip: the user copies the directory into ``userdata/`` manually and then
    registers it via :http:post:`/projects/register`.
    """
    return ok(project_service.list_unregistered_dirs())


@router.post("/register", status_code=201)
async def register_project(body: ProjectRegister):
    """Register an existing ``userdata/`` subdirectory as a project."""
    data = await project_service.register_project(body.directory, body.description or "")
    return ok(data)


@router.get("/{project_id}")
async def get_project(project_id: str):
    data = await project_service.get_project(project_id)
    return ok(data)


@router.patch("/{project_id}")
async def update_project(project_id: str, project: ProjectUpdate):
    data = await project_service.update_project(project_id, project.model_dump(exclude_unset=True))
    return ok(data)


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    await project_service.delete_project(project_id)
    return ok(None)


@router.delete("/{project_id}/database")
async def reset_project_database(project_id: str):
    """Delete the project's database so it is recreated fresh on next access."""
    await project_service.reset_database(project_id)
    return ok(None)


@router.get("/{project_id}/export")
async def export_project(project_id: str):
    """Export a project as a ZIP file (binary download, exempt from unified format)."""
    zip_data = await file_service.create_zip(project_id)
    project = await project_service.get_project(project_id)
    filename = f"{project.get('name', project_id)}.zip"
    return StreamingResponse(
        iter([zip_data]),
        media_type="application/zip",
        headers=download_headers(filename),
    )


@router.get("/{project_id}/config")
async def get_config(project_id: str):
    """Get project configuration."""
    return ok(await project_service.get_project_config(project_id))


@router.patch("/{project_id}/config")
async def update_config(project_id: str, data: ProjectConfigUpdate):
    """Update project configuration."""
    await project_service.update_project_config(project_id, data)
    return ok(None)
