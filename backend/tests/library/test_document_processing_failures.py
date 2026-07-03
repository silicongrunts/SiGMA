"""
Tests for document processing failure paths.

Covers: Docling conversion failure, AI extraction failure (non-fatal),
and index permanent error failure.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.core.document_status import STATUS_FAILED, STATUS_INDEXING
from app.core.exceptions import (
    DocumentConversionError, AIExtractionError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_doc(content="test content", file_path=None, description="",
              keywords=None, source="user upload", title="Test"):
    """Return a dict simulating library_service.get_document() output.

    ``keywords`` follows the post-serialization contract: a list (or None,
    which normalizes to an empty list) — never a JSON string.
    """
    return {
        "id": "doc1",
        "content": content,
        "file_path": file_path,
        "description": description,
        "keywords": keywords or [],
        "source": source,
        "title": title,
    }


def test_ai_metadata_result_discards_unsupported_fields():
    """AI metadata extraction only accepts the supported field contract."""
    from app.services.document_processing_service import DocumentProcessingService

    result = DocumentProcessingService._normalize_ai_metadata_result({
        "title": "A title",
        "description": "A description",
        "keywords": ["alpha"],
        "source": "https://example.test",
    })

    assert result == {
        "title": "A title",
        "description": "A description",
        "keywords": ["alpha"],
    }


@pytest.fixture
def mock_library_service():
    """Patch library_service methods used by _run_processing_logic."""
    with patch("app.services.library_service.library_service") as mock_ls:
        mock_ls.mark_document_processing = AsyncMock()
        mock_ls.mark_document_indexing = AsyncMock()
        mock_ls.mark_document_completed = AsyncMock()
        mock_ls.mark_document_failed = AsyncMock()
        mock_ls.append_processing_log = AsyncMock()
        mock_ls.update_document_content = AsyncMock()
        mock_ls.update_document_fields = AsyncMock()
        mock_ls.get_document = AsyncMock()
        yield mock_ls


@pytest.fixture
def dps(mock_library_service):
    """Create a DocumentProcessingService with mocked deps."""
    with patch("app.services.background_task_service.background_task_service") as mock_bg:
        mock_bg.enqueue_rag_index = AsyncMock()
        mock_bg.enqueue_document_process = AsyncMock()
        from app.services.document_processing_service import DocumentProcessingService
        svc = DocumentProcessingService()
        svc._running = True
        yield svc
        svc._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Docling conversion failure → propagates to durable task runner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_docling_conversion_failure_propagates_to_task_runner(dps, mock_library_service):
    """Docling fatal errors bubble up so the task runner owns retries."""
    import tempfile, os
    from pathlib import Path

    # Create a temp PDF file (non-text)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(b"%PDF-1.4 fake")
        tmp_path = f.name

    try:
        # doc has file_path pointing to the temp file
        mock_library_service.get_document.side_effect = [
            _mock_doc(file_path=tmp_path, content=""),  # first load
        ]

        with patch.object(dps, '_should_stop', return_value=False):
            with patch.object(dps, '_convert_with_docling', return_value=""):
                with pytest.raises(DocumentConversionError):
                    await dps._process_document_in_background("proj1", "doc1")

        mock_library_service.mark_document_failed.assert_not_awaited()
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# AI extraction failure (non-fatal) → document still reaches indexing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_extraction_failure_continues_to_indexing(dps, mock_library_service):
    """When AI extraction fails, document should still reach indexing."""
    mock_library_service.get_document.side_effect = [
            _mock_doc(description=""),  # first load: empty description + keywords triggers AI extraction
        ]

    with patch.object(dps, '_should_stop', return_value=False):
        with patch.object(dps, '_extract_fields_with_ai',
                          side_effect=AIExtractionError(doc_id="doc1")):
            await dps._run_processing_logic("proj1", "doc1")

    # Should have logged the AI failure
    log_calls = [str(c) for c in mock_library_service.append_processing_log.call_args_list]
    assert any("non-fatal" in c for c in log_calls)

    # Should have reached indexing
    mock_library_service.mark_document_indexing.assert_awaited_once()


# ---------------------------------------------------------------------------
# Document with no content → graceful empty handling (not failed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_document_graceful_handling(dps, mock_library_service):
    """Empty document (no file, no content) should reach indexing, not fail."""
    mock_library_service.get_document.return_value = _mock_doc(content="", file_path=None)

    with patch.object(dps, '_should_stop', return_value=False):
        await dps._run_processing_logic("proj1", "doc1")

    # Should NOT be marked failed — instead indexed as empty
    mock_library_service.mark_document_failed.assert_not_awaited()
    mock_library_service.mark_document_indexing.assert_awaited_once()


# ---------------------------------------------------------------------------
# Timeout → propagates to durable task runner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_propagates_to_task_runner(dps, mock_library_service):
    """When processing times out, the task runner owns retries/final failure."""
    import asyncio

    with patch.object(dps, '_should_stop', return_value=False):
        with patch.object(dps, '_run_processing_logic',
                          side_effect=asyncio.TimeoutError()):
            with pytest.raises(TimeoutError, match="Processing timed out"):
                await dps._process_document_in_background("proj1", "doc1")

    mock_library_service.mark_document_failed.assert_not_awaited()


# ---------------------------------------------------------------------------
# Unexpected error → propagates to durable task runner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unexpected_error_propagates_to_task_runner(dps, mock_library_service):
    """Unexpected errors bubble up so durable task state stays authoritative."""
    with patch.object(dps, '_should_stop', return_value=False):
        with patch.object(dps, '_run_processing_logic',
                          side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                await dps._process_document_in_background("proj1", "doc1")

    mock_library_service.mark_document_failed.assert_not_awaited()
