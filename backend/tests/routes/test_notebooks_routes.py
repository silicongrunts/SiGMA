"""Notebook route tests, driven through the route handlers.

The routes guard on lazily-provisioned services (``_get_jupyter_service`` /
``_get_nb_service`` raise ``JupyterNotInitializedError`` when Jupyter has not
started). The guards are exercised indirectly by calling the routes with the
underlying service missing or faked — the private helpers are never invoked
directly.
"""

from types import SimpleNamespace

import pytest

from app.core.exceptions import JupyterNotInitializedError
from app.models.requests import CreateNotebookRequest, NotebookWriteRequest
from app.routes import notebooks
from app.services import notebook_service as nb_service_module


@pytest.mark.route
@pytest.mark.asyncio
async def test_kill_kernel_requires_jupyter_service(monkeypatch):
    """With Jupyter never started, the route raises the not-initialized
    error instead of touching a missing service."""
    monkeypatch.setattr(notebooks, "get_jupyter", lambda: None)

    with pytest.raises(JupyterNotInitializedError):
        await notebooks.kill_kernel("kernel-1")


@pytest.mark.route
@pytest.mark.asyncio
async def test_write_notebook_requires_notebook_service(monkeypatch):
    monkeypatch.setattr(nb_service_module, "notebook_service", None)

    with pytest.raises(JupyterNotInitializedError):
        await notebooks.write_notebook(
            "project-1",
            NotebookWriteRequest(path="analysis.ipynb", notebook={"cells": []}),
        )


@pytest.mark.route
@pytest.mark.asyncio
async def test_kill_kernel_returns_soft_status_when_jupyter_stopped(monkeypatch):
    async def is_running():
        return False

    monkeypatch.setattr(
        notebooks,
        "get_jupyter",
        lambda: SimpleNamespace(is_running=is_running),
    )

    result = await notebooks.kill_kernel("kernel-1")

    assert result["data"] == {"kernel_id": "kernel-1", "detail": "Jupyter not running"}


@pytest.mark.route
@pytest.mark.asyncio
async def test_write_notebook_passes_path_and_payload(monkeypatch):
    calls = {}

    async def write(project_id, path, notebook):
        calls["write"] = (project_id, path, notebook)
        return {"path": path}

    monkeypatch.setattr(nb_service_module, "notebook_service", SimpleNamespace(write=write))

    result = await notebooks.write_notebook(
        "project-1",
        NotebookWriteRequest(path="analysis.ipynb", notebook={"cells": []}),
    )

    assert result["data"] == {"path": "analysis.ipynb"}
    assert calls["write"] == ("project-1", "analysis.ipynb", {"cells": []})


@pytest.mark.route
@pytest.mark.asyncio
async def test_create_notebook_passes_path(monkeypatch):
    async def create_empty(project_id, path):
        return {"project_id": project_id, "path": path}

    monkeypatch.setattr(
        nb_service_module,
        "notebook_service",
        SimpleNamespace(create_empty=create_empty),
    )

    result = await notebooks.create_notebook(
        "project-1",
        CreateNotebookRequest(path="new.ipynb"),
    )

    assert result["data"] == {"project_id": "project-1", "path": "new.ipynb"}
