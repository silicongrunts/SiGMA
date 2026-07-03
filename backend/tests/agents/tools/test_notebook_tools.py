"""Tests for notebook read/edit/run tool behavior."""

import json
import os
from types import SimpleNamespace

import pytest

from app.agents.tools.notebook_tools import (
    _notebook_edit,
    _notebook_read,
    _notebook_run_cell,
)
from app.agents.tools.read_state import read_state_cache


@pytest.fixture(autouse=True)
def _clear_read_state():
    read_state_cache.clear("sess")
    yield
    read_state_cache.clear("sess")


def _patch_project(monkeypatch, tmp_path):
    from app.agents.tools import notebook_tools
    from app.agents.tools import notebook_utils

    monkeypatch.setattr(
        notebook_utils,
        "settings",
        SimpleNamespace(get_project_path=lambda pid: tmp_path),
    )
    monkeypatch.setattr(notebook_utils, "get_jupyter", lambda: None)
    monkeypatch.setattr(notebook_tools, "get_jupyter", lambda: None)


def _write_notebook(path, cells):
    path.write_text(
        json.dumps({
            "cells": cells,
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }),
        encoding="utf-8",
    )


def _cell(cell_id, source, outputs=None, cell_type="code"):
    cell = {
        "id": cell_id,
        "cell_type": cell_type,
        "metadata": {},
        "source": source,
    }
    if cell_type == "code":
        cell["execution_count"] = None
        cell["outputs"] = outputs or []
    return cell


@pytest.mark.asyncio
async def test_notebook_read_pages_cells_and_includes_index(tmp_path, monkeypatch):
    _patch_project(monkeypatch, tmp_path)
    nb = tmp_path / "nb.ipynb"
    _write_notebook(nb, [_cell(f"c{i}", f"print({i})") for i in range(8)])

    result = await _notebook_read("nb.ipynb", "proj", "sess", offset=2, limit=3)

    assert '<notebook cells="8" offset="2" limit="3"' in result
    assert '<cell id="c2" type="code" index="2">' in result
    assert '<cell id="c4" type="code" index="4">' in result
    assert 'id="c1"' not in result
    assert 'id="c5"' not in result


@pytest.mark.asyncio
async def test_notebook_read_cell_id_pages_outputs(tmp_path, monkeypatch):
    _patch_project(monkeypatch, tmp_path)
    nb = tmp_path / "nb.ipynb"
    text = "\n".join(f"line {i}" for i in range(20))
    _write_notebook(nb, [_cell("c1", "print('x')", [{
        "output_type": "stream",
        "name": "stdout",
        "text": text,
    }])])

    result = await _notebook_read(
        "nb.ipynb", "proj", "sess", cell_id="c1", offset=10, limit=5,
    )

    assert '<notebook cells="1" cell_id="c1" offset="10" limit="5"' in result
    assert '<output_excerpt offset="10" lines="5">' in result
    assert "line 10" in result
    assert "line 14" in result
    assert "line 9" not in result
    assert "line 15" not in result


@pytest.mark.asyncio
async def test_notebook_read_truncates_outputs_in_cell_list(tmp_path, monkeypatch):
    _patch_project(monkeypatch, tmp_path)
    nb = tmp_path / "nb.ipynb"
    text = "\n".join(f"line {i}" for i in range(120))
    _write_notebook(nb, [_cell("c1", "print('x')", [{
        "output_type": "stream",
        "name": "stdout",
        "text": text,
    }])])

    result = await _notebook_read("nb.ipynb", "proj", "sess")

    assert 'omitted_lines="20"' in result
    assert 'Use notebook_read with cell_id="c1" offset="100"' in result


@pytest.mark.asyncio
async def test_notebook_edit_requires_prior_read(tmp_path, monkeypatch):
    _patch_project(monkeypatch, tmp_path)
    nb = tmp_path / "nb.ipynb"
    _write_notebook(nb, [_cell("c1", "old")])

    result = await _notebook_edit(
        "nb.ipynb", "new", "proj", "sess", cell_id="c1",
    )

    assert result.startswith("Error:")
    assert "has not been read" in result


@pytest.mark.asyncio
async def test_notebook_edit_succeeds_after_read_and_refreshes_cache(tmp_path, monkeypatch):
    _patch_project(monkeypatch, tmp_path)
    nb = tmp_path / "nb.ipynb"
    _write_notebook(nb, [_cell("c1", "old"), _cell("c2", "second")])

    await _notebook_read("nb.ipynb", "proj", "sess")
    first = await _notebook_edit(
        "nb.ipynb", "new", "proj", "sess", cell_id="c1",
    )
    second = await _notebook_edit(
        "nb.ipynb", "fresh", "proj", "sess", cell_id="c2",
    )

    assert first.startswith("Updated cell")
    assert second.startswith("Updated cell")
    saved = json.loads(nb.read_text(encoding="utf-8"))
    assert saved["cells"][0]["source"] == "new"
    assert saved["cells"][1]["source"] == "fresh"


@pytest.mark.asyncio
async def test_notebook_edit_fails_after_external_change(tmp_path, monkeypatch):
    _patch_project(monkeypatch, tmp_path)
    nb = tmp_path / "nb.ipynb"
    _write_notebook(nb, [_cell("c1", "old")])

    await _notebook_read("nb.ipynb", "proj", "sess")
    _write_notebook(nb, [_cell("c1", "external")])
    st = nb.stat()
    os.utime(nb, (st.st_atime, st.st_mtime + 5))

    result = await _notebook_edit(
        "nb.ipynb", "new", "proj", "sess", cell_id="c1",
    )

    assert result.startswith("Error:")
    assert "has not been read" in result


class _FakeJupyter:
    async def is_running(self):
        return True

    async def get_notebook(self, _path):
        return None

    async def save_notebook(self, _path, _notebook):
        return False

    async def get_session_for_notebook(self, _path, create=False):
        return {"kernel": {"id": "kernel-1"}}

    async def get_kernel_status(self, _kernel_id):
        return {"execution_state": "idle"}

    async def execute_code(self, _kernel_id, _source, timeout=60.0):
        return {
            "status": "ok",
            "execution_count": 1,
            "outputs": [{
                "output_type": "stream",
                "name": "stdout",
                "text": "done",
            }],
        }


@pytest.mark.asyncio
async def test_notebook_run_cell_requires_prior_read(tmp_path, monkeypatch):
    _patch_project(monkeypatch, tmp_path)
    from app.agents.tools import notebook_tools

    monkeypatch.setattr(notebook_tools, "get_jupyter", lambda: _FakeJupyter())
    nb = tmp_path / "nb.ipynb"
    _write_notebook(nb, [_cell("c1", "print('x')")])

    result = await _notebook_run_cell(
        "nb.ipynb", "c1", "proj", "sess",
    )

    assert result.startswith("Error:")
    assert "has not been read" in result


@pytest.mark.asyncio
async def test_notebook_run_cell_succeeds_after_read_and_refreshes_cache(tmp_path, monkeypatch):
    _patch_project(monkeypatch, tmp_path)
    from app.agents.tools import notebook_tools

    monkeypatch.setattr(notebook_tools, "get_jupyter", lambda: _FakeJupyter())
    nb = tmp_path / "nb.ipynb"
    _write_notebook(nb, [_cell("c1", "print('x')")])

    await _notebook_read("nb.ipynb", "proj", "sess")
    result = await _notebook_run_cell(
        "nb.ipynb", "c1", "proj", "sess",
    )
    edit_result = await _notebook_edit(
        "nb.ipynb", "changed", "proj", "sess", cell_id="c1",
    )

    assert "[ok] Execution count: 1" in result
    assert edit_result.startswith("Updated cell")
