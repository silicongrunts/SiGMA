from types import SimpleNamespace

import pytest

from app.models.requests import CompileRequest, SyncTeXRequest
from app.routes import compile as compile_routes


@pytest.mark.route
@pytest.mark.asyncio
async def test_compile_latex_passes_engine_and_main_file(monkeypatch):
    calls = {}

    async def compile_project(project_id, main_file, engine):
        calls["compile"] = (project_id, main_file, engine)
        return {"ok": True}

    monkeypatch.setattr(
        compile_routes,
        "latex_service",
        SimpleNamespace(compile_project=compile_project),
    )

    result = await compile_routes.compile_latex(
        "project-1",
        CompileRequest(main_file="src/main.tex", engine="xelatex"),
    )

    assert result["data"] == {"ok": True}
    assert calls["compile"] == ("project-1", "src/main.tex", "xelatex")


@pytest.mark.route
@pytest.mark.asyncio
async def test_synctex_passes_request_object(monkeypatch):
    calls = {}

    async def synctex(project_id, request):
        calls["synctex"] = (project_id, request)
        return {"file": "main.tex"}

    monkeypatch.setattr(compile_routes, "latex_service", SimpleNamespace(synctex=synctex))
    request = SyncTeXRequest(type="backward", page=1, x=10.0, y=20.0)

    result = await compile_routes.synctex("project-1", request)

    assert result["data"] == {"file": "main.tex"}
    assert calls["synctex"] == ("project-1", request)
