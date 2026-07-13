"""Tests for annotation tools — sandbox containment and error surfacing.

Covers:
- ``_ensure_inside_sandbox`` rejects paths outside the project sandbox before
  any file access is attempted.
- ``_annotation_new`` surfaces an explicit error for external paths instead of
  a generic "file not found".
- ``_annotation_list`` likewise rejects external paths per file.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.tools.annotation_tools import (
    _annotation_list, _annotation_new, _ensure_inside_sandbox,
)
from app.core.exceptions import FileSystemError


def _patch_file_service(monkeypatch, tmp_path):
    """Point file_service at a fake project sandbox rooted at tmp_path."""
    from app.services import file_service

    monkeypatch.setattr(
        file_service.file_service, "get_project_path",
        lambda pid: tmp_path,
    )


# ── _ensure_inside_sandbox ──────────────────────────────────────────

def test_ensure_inside_sandbox_accepts_project_relative(monkeypatch, tmp_path):
    _patch_file_service(monkeypatch, tmp_path)
    # Should not raise — path resolves inside the sandbox.
    _ensure_inside_sandbox("proj", "notes.md")


def test_ensure_inside_sandbox_rejects_absolute_external(monkeypatch, tmp_path):
    _patch_file_service(monkeypatch, tmp_path)
    with pytest.raises(FileSystemError) as exc:
        _ensure_inside_sandbox("proj", "/home/x.md")
    assert "outside the project" in str(exc.value) or "current project" in str(exc.value)


def test_ensure_inside_sandbox_rejects_forbidden(monkeypatch, tmp_path):
    _patch_file_service(monkeypatch, tmp_path)
    with pytest.raises(FileSystemError) as exc:
        _ensure_inside_sandbox("proj", "/etc/passwd")
    assert "current project" in str(exc.value)


def test_ensure_inside_sandbox_rejects_traversal(monkeypatch, tmp_path):
    _patch_file_service(monkeypatch, tmp_path)
    with pytest.raises(FileSystemError):
        _ensure_inside_sandbox("proj", "../../etc/passwd")


# ── _annotation_new ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_annotation_new_external_path_returns_error(monkeypatch, tmp_path):
    """An external file_path must yield a clear error, never a dialog."""
    _patch_file_service(monkeypatch, tmp_path)
    result = await _annotation_new(
        project_id="proj",
        file_name="/home/x.md",
        file_content="root",
        annotation_content="check this",
    )
    assert "current project" in result


@pytest.mark.asyncio
async def test_annotation_new_sandbox_file_creates_annotation(monkeypatch, tmp_path):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "notes.md").write_text("hello world\n")
    with patch("app.agents.tools.annotation_tools.annotation_service.add_annotation",
               new=AsyncMock(return_value={"id": "anno-1"})):
        result = await _annotation_new(
            project_id="proj",
            file_name="notes.md",
            file_content="hello",
            annotation_content="greeting",
        )
    assert "anno-1" in result


# ── _annotation_list ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_annotation_list_external_path_returns_error(monkeypatch, tmp_path):
    _patch_file_service(monkeypatch, tmp_path)
    result = await _annotation_list(project_id="proj", file_name="/home/x.md")
    assert "current project" in result


@pytest.mark.asyncio
async def test_annotation_list_sandbox_file_no_annos(monkeypatch, tmp_path):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "notes.md").write_text("hello\n")
    with patch("app.agents.tools.annotation_tools.annotation_service.list_annotations_by_file",
               new=AsyncMock(return_value=[])):
        result = await _annotation_list(project_id="proj", file_name="notes.md")
    assert "No annotations" in result
