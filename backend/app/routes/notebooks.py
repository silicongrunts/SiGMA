from __future__ import annotations

from fastapi import APIRouter

from app.models.requests import NotebookWriteRequest, CreateNotebookRequest
from app.core.exceptions import (
    JupyterNotInitializedError,
)
from ..services import notebook_service as nb_service_module
from ..services.jupyter_service import JupyterService, get_jupyter
from app.core.response import ok

router = APIRouter(prefix="/notebooks", tags=["Notebooks"])


def _get_jupyter_service() -> JupyterService:
    svc = get_jupyter()
    if svc is None:
        raise JupyterNotInitializedError()
    return svc


def _get_nb_service():
    if nb_service_module.notebook_service is None:
        raise JupyterNotInitializedError("Notebook service not initialized")
    return nb_service_module.notebook_service


# ============================================================================
# Static routes MUST come BEFORE parameterized routes
# ============================================================================

@router.get("/kernels")
async def list_kernels():
    """List all active Jupyter kernels with enhanced labels."""
    jupyter_svc = _get_jupyter_service()
    result = await jupyter_svc.list_kernels_enriched()
    return ok(result)


@router.delete("/kernels/{kernel_id}")
async def kill_kernel(kernel_id: str):
    """Kill a specific Jupyter kernel."""
    jupyter_svc = _get_jupyter_service()
    if not await jupyter_svc.is_running():
        return ok({"kernel_id": kernel_id, "detail": "Jupyter not running"})
    await jupyter_svc.kill_kernel(kernel_id)
    return ok({"kernel_id": kernel_id})


# ============================================================================
# Parameterized routes — MUST come after static
# ============================================================================

@router.get("/{project_id}/url")
async def get_jupyter_url(project_id: str, path: str):
    svc = _get_jupyter_service()
    nb_service = _get_nb_service()
    safe_path = nb_service.get_project_relative_path(project_id, path)
    if not await svc.is_running():
        await svc.start()
    url = svc.get_url(f"{project_id}/{safe_path}")
    return ok({"url": url})


@router.get("/{project_id}")
async def read_notebook(project_id: str, path: str):
    nb_service = _get_nb_service()
    notebook = await nb_service.read(project_id, path)
    return ok({"project_id": project_id, "path": path, "notebook": notebook})


@router.post("/{project_id}")
async def write_notebook(project_id: str, req: NotebookWriteRequest):
    nb_service = _get_nb_service()
    data = await nb_service.write(project_id, req.path, req.notebook)
    return ok(data)


@router.post("/{project_id}/create")
async def create_notebook(project_id: str, req: CreateNotebookRequest):
    nb_service = _get_nb_service()
    data = await nb_service.create_empty(project_id, req.path)
    return ok(data)
