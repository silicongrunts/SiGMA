from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.models.requests import CompileRequest, SyncTeXRequest
from app.services.latex_service import latex_service
from app.core.response import ok

router = APIRouter(prefix="/compile", tags=["compile"])


@router.post("/{project_id}")
async def compile_latex(project_id: str, request: CompileRequest):
    """Compile a LaTeX project."""
    result = await latex_service.compile_project(project_id, request.main_file, request.engine)
    return ok(result)


@router.post("/{project_id}/synctex")
async def synctex(project_id: str, request: SyncTeXRequest):
    """Perform SyncTeX mapping."""
    result = await latex_service.synctex(project_id, request)
    return ok(result)


@router.get("/{project_id}/pdf")
async def get_pdf(project_id: str, filename: Optional[str] = None):
    """Get the compiled PDF file (binary download, exempt from unified format)."""
    if not filename:
        filename = await latex_service.get_pdf_filename(project_id)
    pdf_path = latex_service.get_pdf_path(project_id, filename)
    return FileResponse(
        path=pdf_path, media_type="application/pdf", filename=Path(filename).name,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.get("/{project_id}/status")
async def get_compile_status(project_id: str):
    """Get the compilation status."""
    return ok(await latex_service.get_compile_status(project_id))
