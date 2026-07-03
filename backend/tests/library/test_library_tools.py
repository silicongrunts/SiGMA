"""
Tests for library_tools — focused on the contract changes:

- _library_new title-required + extension whitelist + source format + rollback
- _copy_unique_file max_attempts bound
- _library_update folder rules (title-only)

These tests stub library_service so they don't need a real DB. Service-layer
atomicity (TOCTOU) and target-existence checks are covered separately by
service-level integration tests.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.agents.tools.library_tools import (
    _library_new, _library_update, _library_search, _copy_unique_file,
)
from app.core.exceptions import FileSystemError, RAGIndexModelMismatchError, ValidationError


# ---------------------------------------------------------------------------
# _library_new — title required
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_library_new_rejects_empty_title():
    """title is required by the tool contract, not just the schema."""
    result = await _library_new("proj", "text", "hello world", title="")
    assert "title is required" in result


@pytest.mark.asyncio
async def test_library_new_rejects_whitespace_title():
    result = await _library_new("proj", "text", "hello world", title="   ")
    assert "title is required" in result


@pytest.mark.asyncio
async def test_library_new_rejects_empty_content():
    result = await _library_new("proj", "text", "   ", title="t")
    assert "content cannot be empty" in result


# ---------------------------------------------------------------------------
# _library_new — extension whitelist (no-extension rejected)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_library_new_rejects_no_extension_file(tmp_path):
    """Files without an extension must be rejected at the tool boundary;
    they would otherwise hit a Docling dead path in the worker."""
    no_ext = tmp_path / "README"
    no_ext.write_text("no extension")

    with patch("app.agents.tools.library_tools.settings") as mock_settings:
        mock_settings.get_project_path.return_value = tmp_path
        result = await _library_new(
            "proj", "file", str(no_ext), title="t",
        )
    assert "unsupported file type" in result
    assert "no extension" in result


@pytest.mark.asyncio
async def test_library_new_rejects_disallowed_extension(tmp_path):
    bad = tmp_path / "evil.exe"
    bad.write_text("binary")

    with patch("app.agents.tools.library_tools.settings") as mock_settings:
        mock_settings.get_project_path.return_value = tmp_path
        result = await _library_new(
            "proj", "file", str(bad), title="t",
        )
    assert "unsupported file type" in result
    assert ".exe" in result


# ---------------------------------------------------------------------------
# _library_new — source field format (relative vs absolute)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_library_new_source_relative_for_project_internal_file(tmp_path):
    """Files imported from inside the project record source as a relative path."""
    project_root = tmp_path
    notes_dir = project_root / "notes"
    notes_dir.mkdir()
    src = notes_dir / "a.txt"
    src.write_text("hello")

    captured_source = {}

    async def fake_create(proj, **kw):
        captured_source["source"] = kw.get("source")
        doc = MagicMock()
        doc.id = "abc12345-0000-0000-0000-000000000000"
        return doc

    sigma_dir = project_root / ".SiGMA"

    with patch("app.agents.tools.library_tools.settings") as mock_settings, \
         patch("app.agents.tools.library_tools.library_service") as mock_svc, \
         patch("app.agents.tools.library_tools.background_task_service", create=True), \
         tempfile.TemporaryDirectory() as lib_tmp:
        mock_settings.get_project_path.return_value = project_root
        mock_settings.get_sigma_path.return_value = Path(lib_tmp)
        mock_svc.create_library_document = fake_create
        # enqueue_document_process is imported lazily inside _library_new
        import app.services.background_task_service as bts
        with patch.object(bts, "background_task_service") as mock_bts:
            mock_bts.enqueue_document_process = AsyncMock()
            await _library_new("proj", "file", "notes/a.txt", title="t")

    # Source should be the relative path, not the absolute one
    assert captured_source["source"] == "notes/a.txt"


@pytest.mark.asyncio
async def test_library_new_source_absolute_for_project_external_file(tmp_path):
    """Files imported from outside the project record source as an absolute path."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("outside")

    captured_source = {}

    async def fake_create(proj, **kw):
        captured_source["source"] = kw.get("source")
        doc = MagicMock()
        doc.id = "abc12345-0000-0000-0000-000000000000"
        return doc

    with patch("app.agents.tools.library_tools.settings") as mock_settings, \
         patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_settings.get_project_path.return_value = project_root
        mock_settings.get_sigma_path.return_value = tmp_path / ".SiGMA"
        mock_svc.create_library_document = fake_create
        import app.services.background_task_service as bts
        with patch.object(bts, "background_task_service") as mock_bts:
            mock_bts.enqueue_document_process = AsyncMock()
            await _library_new("proj", "file", str(external), title="t")

    # Source should be the absolute path
    assert captured_source["source"] == str(external)


# ---------------------------------------------------------------------------
# _library_new — DB failure rolls back the copied file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_library_new_db_failure_rolls_back_file(tmp_path):
    """If create_library_document raises, the file we copied into the library
    directory must be unlinked so we don't leak orphans."""
    src = tmp_path / "src.txt"
    src.write_text("payload")

    lib_dir = tmp_path / ".SiGMA" / "library"

    async def fake_create(proj, **kw):
        # The file should exist at this point — we're inside the try block.
        saved = kw.get("file_path")
        assert saved and Path(saved).exists(), "copied file should exist before DB failure"
        raise RuntimeError("simulated DB failure")

    with patch("app.agents.tools.library_tools.settings") as mock_settings, \
         patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_settings.get_project_path.return_value = tmp_path
        mock_settings.get_sigma_path.return_value = tmp_path / ".SiGMA"
        mock_svc.create_library_document = fake_create

        result = await _library_new("proj", "file", str(src), title="t")

    assert "Failed to add document" in result
    # The orphan must be gone.
    if lib_dir.exists():
        leftovers = [p for p in lib_dir.iterdir() if p.is_file()]
        assert not leftovers, f"orphan files left behind: {leftovers}"


# ---------------------------------------------------------------------------
# _copy_unique_file — max_attempts bound
# ---------------------------------------------------------------------------

def test_copy_unique_file_raises_after_max_attempts(tmp_path):
    """The dedupe loop is bounded; running out of attempts raises instead of
    looping forever."""
    src = tmp_path / "src.txt"
    src.write_text("payload")
    target = tmp_path / "out.txt"

    # Pre-create out.txt, out_1.txt, ... out_100.txt so the loop runs out.
    target.write_text("x")
    for i in range(1, 101):
        (tmp_path / f"out_{i}.txt").write_text("x")

    with pytest.raises(FileSystemError) as exc_info:
        _copy_unique_file(src, target)
    assert "100 attempts" in str(exc_info.value)


def test_copy_unique_file_succeeds_when_slot_free(tmp_path):
    """Sanity check: when the slot is free, the copy succeeds."""
    src = tmp_path / "src.txt"
    src.write_text("payload")
    target = tmp_path / "out.txt"
    result = _copy_unique_file(src, target)
    assert result.exists()
    assert result.read_text() == "payload"


def test_copy_unique_file_appends_suffix_on_collision(tmp_path):
    """A collision appends _1 rather than overwriting the existing file."""
    src = tmp_path / "src.txt"
    src.write_text("payload")
    target = tmp_path / "out.txt"
    target.write_text("original")

    result = _copy_unique_file(src, target)
    assert result.name == "out_1.txt"
    assert result.read_text() == "payload"
    assert target.read_text() == "original"  # original untouched


# ---------------------------------------------------------------------------
# _library_search — service-provided keyword matches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_library_search_keeps_title_keyword_matches():
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.search_documents_paged = AsyncMock(return_value={
            "results": [{
                "id": "abcdef12-0000-0000-0000-000000000000",
                "title": "Quantum Atlas",
                "description": "",
                "keywords": [],
                "search_matches": [{
                    "field": "title",
                    "text": "Quantum Atlas",
                    "line": 1,
                }],
            }],
            "total": 1,
        })
        mock_svc.get_documents_by_ids = AsyncMock()

        result = await _library_search("proj", "Quantum", mode="keyword")

    assert "<title>Quantum Atlas</title>" in result
    assert 'field="title"' in result
    assert 'line="1"' in result
    mock_svc.get_documents_by_ids.assert_not_called()


@pytest.mark.asyncio
async def test_library_search_translates_page_to_db_offset_and_limit():
    """page=3 with PAGE_SIZE=50 must hit the service with offset=100, limit=50."""
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.search_documents_paged = AsyncMock(return_value={
            "results": [], "total": 0,
        })
        await _library_search("proj", "foo", mode="keyword", page=3)

    call = mock_svc.search_documents_paged.call_args
    assert call.kwargs["offset"] == 100
    assert call.kwargs["limit"] == 50


@pytest.mark.asyncio
async def test_library_search_rejects_page_below_one():
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        result = await _library_search("proj", "foo", mode="keyword", page=0)
    assert "page must be >= 1" in result
    mock_svc.search_documents_paged.assert_not_called()


@pytest.mark.asyncio
async def test_library_search_header_reflects_real_total_and_window():
    """Header shows real total + page window; no 'browsable' cap wording."""
    entry = {
        "id": "abcdef12-0000-0000-0000-000000000000",
        "title": "T", "description": "", "keywords": [],
        "search_matches": [{"field": "title", "text": "T", "line": 1}],
    }
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.search_documents_paged = AsyncMock(return_value={
            "results": [entry], "total": 250,
        })
        # page=3 → positions 101-150; service returns 1 entry → window 101-101
        result = await _library_search("proj", "T", mode="keyword", page=3)
    assert "Found 250 result(s)" in result
    assert "showing 101-101" in result
    assert "browsable" not in result


@pytest.mark.asyncio
async def test_library_search_semantic_mode_omits_window_header():
    """Semantic mode is not paginated — header must be a plain count,
    and total must reflect the actual returned chunk count, not 0."""
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.rag_search = AsyncMock(return_value=[{
            "id": "abcdef12-0000-0000-0000-000000000000",
            "title": "Sem", "description": "", "keywords": [],
            "chunk_text": "snippet", "chunk_line_start": 7,
            "relevance_score": 0.8,
        }])
        result = await _library_search("proj", "snippet", mode="semantic")
    assert "Found 1 result(s):" in result
    assert "showing" not in result
    assert "browsable" not in result
    assert 'line="7"' in result
    assert 'score="0.8"' in result


@pytest.mark.asyncio
async def test_library_search_semantic_model_mismatch_asks_user_to_rebuild():
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.rag_search = AsyncMock(side_effect=RAGIndexModelMismatchError(
            current_model="new-model",
            indexed_model="old-model",
        ))
        result = await _library_search("proj", "snippet", mode="semantic")

    assert result.startswith("Error: Embedding model changed")
    assert "Ask the user to rebuild the Library RAG index" in result
    assert "semantic search" in result


@pytest.mark.asyncio
async def test_library_search_keyword_page_out_of_range_surfaces_real_total():
    """When page is beyond the last page, the message must NOT say 'No results'
    — it must surface the real count so the LLM knows matches exist."""
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.search_documents_paged = AsyncMock(return_value={
            "results": [], "total": 250,
        })
        result = await _library_search("proj", "foo", mode="keyword", page=999)
    assert "No results" not in result
    assert "Found 250 result(s)" in result
    assert "out of range" in result
    assert "last page" in result


@pytest.mark.asyncio
async def test_library_search_keyword_truly_no_matches_says_no_results():
    """When total == 0, the message is the plain 'No results' form."""
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.search_documents_paged = AsyncMock(return_value={
            "results": [], "total": 0,
        })
        result = await _library_search("proj", "foo", mode="keyword")
    assert result == "No results for 'foo'"


@pytest.mark.asyncio
async def test_library_search_semantic_mode_ignores_invalid_page():
    """prompt says semantic ignores page; the page<1 guard must NOT fire
    when mode is semantic (it only applies to keyword mode)."""
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.rag_search = AsyncMock(return_value=[{
            "id": "abcdef12-0000-0000-0000-000000000000",
            "title": "Sem", "description": "", "keywords": [],
            "chunk_text": "x", "chunk_line_start": 1,
            "relevance_score": 0.5,
        }])
        # page=0 in semantic mode should NOT error
        result = await _library_search("proj", "x", mode="semantic", page=0)
    assert "page must be >= 1" not in result
    assert "Found 1 result(s):" in result


@pytest.mark.asyncio
async def test_library_search_keyword_mode_still_enforces_page_lower_bound():
    """page<1 in keyword mode must still error."""
    with patch("app.agents.tools.library_tools.library_service") as mock_svc:
        result = await _library_search("proj", "foo", mode="keyword", page=0)
    assert "page must be >= 1" in result
    mock_svc.search_documents_paged.assert_not_called()


# ---------------------------------------------------------------------------
# _library_update — folder rules (title-only)
# ---------------------------------------------------------------------------

def _make_folder_doc():
    """Build a mock doc that looks like a folder."""
    doc = MagicMock()
    doc.id = "folder-id-1234567890"
    doc.is_folder = True
    doc.title = "OldFolder"
    return doc


@pytest.mark.asyncio
async def test_library_update_folder_rejects_description():
    doc = _make_folder_doc()
    with patch("app.agents.tools.library_tools._resolve_id",
               new=AsyncMock(return_value=(doc, None))):
        result = await _library_update("proj", "abc12345", description="new desc")
    assert "folders only support title" in result.lower()


@pytest.mark.asyncio
async def test_library_update_folder_rejects_content_replace():
    doc = _make_folder_doc()
    with patch("app.agents.tools.library_tools._resolve_id",
               new=AsyncMock(return_value=(doc, None))):
        result = await _library_update(
            "proj", "abc12345",
            old_string="x", new_string="y",
        )
    assert "folders only support title" in result.lower()


@pytest.mark.asyncio
async def test_library_update_folder_allows_title_only():
    """Folder rename should pass through to the service."""
    doc = _make_folder_doc()
    with patch("app.agents.tools.library_tools._resolve_id",
               new=AsyncMock(return_value=(doc, None))), \
         patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.update_document = AsyncMock(return_value={
            "id": doc.id, "title": "NewFolder",
        })
        result = await _library_update("proj", "abc12345", title="NewFolder")
    assert "Successfully updated" in result
    # update_document is called positionally: (project_id, doc_id, updates_dict)
    call_args = mock_svc.update_document.call_args.args
    assert call_args[2] == {"title": "NewFolder"}


@pytest.mark.asyncio
async def test_library_update_passes_old_new_string_to_service():
    """Content edits reach the service as old_string/new_string, NOT as a
    pre-computed content blob. The atomic count+replace happens in the service."""
    doc = MagicMock()
    doc.id = "doc-id-1234567890"
    doc.is_folder = False
    doc.title = "Old"
    with patch("app.agents.tools.library_tools._resolve_id",
               new=AsyncMock(return_value=(doc, None))), \
         patch("app.agents.tools.library_tools.library_service") as mock_svc:
        mock_svc.update_document = AsyncMock(return_value={
            "id": doc.id, "title": "Old",
        })
        await _library_update(
            "proj", "abc12345",
            old_string="foo", new_string="bar",
        )
    call_args = mock_svc.update_document.call_args.args
    assert call_args[2] == {"old_string": "foo", "new_string": "bar"}


# ---------------------------------------------------------------------------
# _library_update — basic param validation stays at tool layer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_library_update_rejects_mismatched_old_new():
    """old_string without new_string (or vice versa) is a param-shape error
    caught at the tool boundary, before the service is called."""
    result = await _library_update("proj", "abc12345", old_string="x")
    assert "must both be provided" in result

    result = await _library_update("proj", "abc12345", new_string="y")
    assert "must both be provided" in result


@pytest.mark.asyncio
async def test_library_update_identical_old_new_rejected():
    doc = MagicMock()
    doc.id = "doc-id"
    doc.is_folder = False
    with patch("app.agents.tools.library_tools._resolve_id",
               new=AsyncMock(return_value=(doc, None))):
        result = await _library_update(
            "proj", "abc12345",
            old_string="same", new_string="same",
        )
    assert "identical" in result
