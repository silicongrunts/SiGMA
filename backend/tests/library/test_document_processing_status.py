"""
Tests for document processing status constants and library_service
status transition methods.
"""

import pytest

from app.core.document_status import (
    STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING,
    STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED,
    ALL_STATUSES, ACTIVE_STATUSES,
)


# ---------------------------------------------------------------------------
# Constant values
# ---------------------------------------------------------------------------

def test_status_values():
    assert STATUS_PENDING == "pending"
    assert STATUS_PROCESSING == "processing"
    assert STATUS_INDEXING == "indexing"
    assert STATUS_CANCELLING == "cancelling"
    assert STATUS_COMPLETED == "completed"
    assert STATUS_FAILED == "failed"


def test_all_statuses_has_six():
    assert len(ALL_STATUSES) == 6
    for s in [STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING,
              STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED]:
        assert s in ALL_STATUSES


def test_active_statuses_are_three():
    assert ACTIVE_STATUSES == {STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING}
    assert STATUS_CANCELLING not in ACTIVE_STATUSES
    assert STATUS_COMPLETED not in ACTIVE_STATUSES
    assert STATUS_FAILED not in ACTIVE_STATUSES


def test_constants_are_strings():
    """All status constants are plain strings (DB stores strings)."""
    for s in ALL_STATUSES:
        assert isinstance(s, str)


# ---------------------------------------------------------------------------
# Status transition methods (mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_document_processing_delegates():
    """mark_document_processing calls update_processing_status with correct args."""
    from unittest.mock import AsyncMock, patch, MagicMock

    with patch("app.services.library_service.UnitOfWork") as MockUoW:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        MockUoW.return_value = mock_uow

        from app.services.library_service import library_service
        await library_service.mark_document_processing("proj1", "doc1")

        mock_uow.library.update_processing_status.assert_awaited_once()
        call_kwargs = mock_uow.library.update_processing_status.call_args[1]
        assert call_kwargs["status"] == STATUS_PROCESSING
        # doc_id is the first positional arg
        call_args = mock_uow.library.update_processing_status.call_args[0]
        assert call_args[0] == "doc1"
        assert "started_at" in call_kwargs


@pytest.mark.asyncio
async def test_mark_document_indexing_delegates():
    """mark_document_indexing calls update_processing_status with INDEXING."""
    from unittest.mock import AsyncMock, patch

    with patch("app.services.library_service.UnitOfWork") as MockUoW:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        MockUoW.return_value = mock_uow

        from app.services.library_service import library_service
        await library_service.mark_document_indexing("proj1", "doc1")

        call_kwargs = mock_uow.library.update_processing_status.call_args[1]
        assert call_kwargs["status"] == STATUS_INDEXING


@pytest.mark.asyncio
async def test_mark_document_failed_delegates():
    """mark_document_failed calls repo.mark_failed."""
    from unittest.mock import AsyncMock, patch

    with patch("app.services.library_service.UnitOfWork") as MockUoW:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        MockUoW.return_value = mock_uow

        from app.services.library_service import library_service
        await library_service.mark_document_failed("proj1", "doc1", "test error")

        mock_uow.library.mark_failed.assert_awaited_once_with("doc1", "test error")


@pytest.mark.asyncio
async def test_append_processing_log_delegates():
    """append_processing_log calls repo.update_processing_log."""
    from unittest.mock import AsyncMock, patch

    with patch("app.services.library_service.UnitOfWork") as MockUoW:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        MockUoW.return_value = mock_uow

        from app.services.library_service import library_service
        await library_service.append_processing_log("proj1", "doc1", "test message")

        mock_uow.library.update_processing_log.assert_awaited_once_with("doc1", "test message")


@pytest.mark.asyncio
async def test_update_document_content_delegates():
    """update_document_content calls repo.update_content."""
    from unittest.mock import AsyncMock, patch

    with patch("app.services.library_service.UnitOfWork") as MockUoW:
        mock_uow = AsyncMock()
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        MockUoW.return_value = mock_uow

        from app.services.library_service import library_service
        await library_service.update_document_content("proj1", "doc1", "new content")

        mock_uow.library.update_content.assert_awaited_once_with("doc1", "new content")
