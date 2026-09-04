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


# ── _ensure_inside_sandbox ──────────────────────────────────────────

def test_ensure_inside_sandbox_accepts_project_relative(project_root):
    # Should not raise — path resolves inside the sandbox.
    _ensure_inside_sandbox("proj", "notes.md")


def test_ensure_inside_sandbox_rejects_absolute_external(project_root):
    with pytest.raises(FileSystemError) as exc:
        _ensure_inside_sandbox("proj", "/home/x.md")
    # Exact product wording — the error names the current project and the
    # offending path, not a generic filesystem failure.
    assert "inside the current project" in str(exc.value)
    assert "/home/x.md" in str(exc.value)


def test_ensure_inside_sandbox_rejects_forbidden(project_root):
    with pytest.raises(FileSystemError) as exc:
        _ensure_inside_sandbox("proj", "/etc/passwd")
    assert "current project" in str(exc.value)


def test_ensure_inside_sandbox_rejects_traversal(project_root):
    with pytest.raises(FileSystemError):
        _ensure_inside_sandbox("proj", "../../etc/passwd")


# ── _annotation_new ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_annotation_new_external_path_returns_error(project_root):
    """An external file_path must yield a clear error, never a dialog."""
    result = await _annotation_new(
        project_id="proj",
        file_name="/home/x.md",
        file_content="root",
        annotation_content="check this",
    )
    assert "current project" in result


@pytest.mark.asyncio
async def test_annotation_new_sandbox_file_creates_annotation(project_root):
    (project_root / "notes.md").write_text("hello world\n")
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
async def test_annotation_list_external_path_returns_error(project_root):
    result = await _annotation_list(project_id="proj", file_name="/home/x.md")
    assert "current project" in result


@pytest.mark.asyncio
async def test_annotation_list_sandbox_file_no_annos(project_root):
    (project_root / "notes.md").write_text("hello\n")
    with patch("app.agents.tools.annotation_tools.annotation_service.list_annotations_by_file",
               new=AsyncMock(return_value=[])):
        result = await _annotation_list(project_id="proj", file_name="notes.md")
    assert "No annotations" in result
