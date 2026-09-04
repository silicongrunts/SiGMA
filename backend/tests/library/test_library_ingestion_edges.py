"""
Tests for library file ingestion edge cases.

Covers: sanitize_filename, unsupported extensions, empty files,
encoding anomalies, and duplicate filename handling.
"""

import pytest
import tempfile
import os
from pathlib import Path
from types import SimpleNamespace

from app.core.utils import sanitize_filename
from app.core.exceptions import FileSystemError
from app.services.document_processing_service import UPLOADABLE_EXTENSIONS
from tests.factories.uploads import ChunkedUpload


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

def test_sanitize_rejects_traversal():
    with pytest.raises(FileSystemError):
        sanitize_filename("../etc/passwd")


def test_sanitize_rejects_hidden():
    with pytest.raises(FileSystemError):
        sanitize_filename(".hidden")


def test_sanitize_rejects_empty():
    with pytest.raises(FileSystemError):
        sanitize_filename("")


def test_sanitize_rejects_path_separator():
    with pytest.raises(FileSystemError):
        sanitize_filename("sub/dir.txt")


def test_sanitize_accepts_normal():
    assert sanitize_filename("report.pdf") == "report.pdf"


def test_sanitize_accepts_spaces():
    assert sanitize_filename("my paper v2.docx") == "my paper v2.docx"


def test_sanitize_accepts_chinese():
    result = sanitize_filename("论文.pdf")
    assert result == "论文.pdf"


# ---------------------------------------------------------------------------
# Extension validation
# ---------------------------------------------------------------------------

def test_txt_is_uploadable():
    assert ".txt" in UPLOADABLE_EXTENSIONS


def test_pdf_is_uploadable():
    assert ".pdf" in UPLOADABLE_EXTENSIONS


def test_exe_not_uploadable():
    assert ".exe" not in UPLOADABLE_EXTENSIONS


def test_bat_not_uploadable():
    assert ".bat" not in UPLOADABLE_EXTENSIONS


# ---------------------------------------------------------------------------
# Text file encoding — driven through the real processing path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_processing_reads_bad_utf8_text_with_replacement_chars():
    """_run_processing_logic's text branch reads with
    ``encoding='utf-8', errors='replace'``: undecodable bytes reach the
    stored content as U+FFFD instead of killing the ingestion task with a
    UnicodeDecodeError."""
    from unittest.mock import AsyncMock, patch

    from app.services.document_processing_service import DocumentProcessingService

    svc = DocumentProcessingService()

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="wb") as f:
        # Invalid UTF-8 sequence
        f.write(b"Hello \xff\xfe World")
        tmp_path = f.name

    try:
        saved = {}
        with patch("app.services.library_service.library_service") as mock_ls, \
             patch("app.services.background_task_service.background_task_service") as mock_bg, \
             patch.object(svc, "_should_stop", AsyncMock(return_value=False)):
            mock_ls.mark_document_processing = AsyncMock(return_value=True)
            mock_ls.mark_document_indexing = AsyncMock(return_value=True)
            mock_ls.append_processing_log = AsyncMock()
            mock_ls.update_document_content = AsyncMock(
                side_effect=lambda _p, _d, content, **_: saved.update(content=content),
            )
            # description + keywords populated so no AI extraction is attempted
            mock_ls.get_document = AsyncMock(return_value={
                "id": "doc1", "content": "", "file_path": tmp_path,
                "description": "already described", "keywords": ["k"],
                "source": "user upload", "title": "T",
            })
            mock_bg.enqueue_rag_index = AsyncMock()

            await svc._run_processing_logic("proj1", "doc1")

        content = saved["content"]
        assert "Hello" in content
        assert "World" in content
        # Replacement chars should appear where the invalid bytes were
        assert "\ufffd" in content
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# DocumentProcessingService.upload_files edge cases (mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upload_skips_unsupported_extension():
    """upload_files returns errors for unsupported extensions."""
    from unittest.mock import AsyncMock, patch
    from app.services.document_processing_service import DocumentProcessingService
    svc = DocumentProcessingService()

    # Create a mock UploadFile
    mock_file = AsyncMock()
    mock_file.filename = "malware.exe"
    mock_file.read = AsyncMock(return_value=b"binary content")
    mock_file.seek = AsyncMock()

    with tempfile.TemporaryDirectory() as tmp:
        sigma_dir = Path(tmp) / ".SiGMA"
        sigma_dir.mkdir()
        library_dir = sigma_dir / "library"
        library_dir.mkdir()

        with patch("app.services.document_processing_service.settings") as mock_settings:
            mock_settings.get_sigma_path.return_value = sigma_dir
            result = await svc.upload_files("fake-project", [mock_file])
            # Should return errors list with the unsupported file
            assert len(result["documents"]) == 0
            assert len(result["errors"]) == 1
            assert "Unsupported file type" in result["errors"][0]["reason"]
            assert result["errors"][0]["file"] == "malware.exe"


@pytest.mark.asyncio
async def test_upload_rejects_invalid_filename():
    """upload_files returns errors for invalid filenames."""
    from unittest.mock import AsyncMock, patch
    from app.services.document_processing_service import DocumentProcessingService
    svc = DocumentProcessingService()

    mock_file = AsyncMock()
    mock_file.filename = "../etc/passwd"
    mock_file.read = AsyncMock(return_value=b"binary content")
    mock_file.seek = AsyncMock()

    with tempfile.TemporaryDirectory() as tmp:
        sigma_dir = Path(tmp) / ".SiGMA"
        sigma_dir.mkdir()
        library_dir = sigma_dir / "library"
        library_dir.mkdir()

        with patch("app.services.document_processing_service.settings") as mock_settings:
            mock_settings.get_sigma_path.return_value = sigma_dir
            result = await svc.upload_files("fake-project", [mock_file])
            assert len(result["documents"]) == 0
            assert len(result["errors"]) == 1
            assert "Invalid filename" in result["errors"][0]["reason"]


def test_upload_relative_path_parser_accepts_nested_path():
    from app.services.document_processing_service import DocumentProcessingService

    directories, filename = DocumentProcessingService._parse_upload_relative_path(
        "papers/2026/report.pdf",
        "report.pdf",
    )

    assert directories == ["papers", "2026"]
    assert filename == "report.pdf"


def test_upload_relative_path_parser_rejects_traversal():
    from app.services.document_processing_service import DocumentProcessingService

    with pytest.raises(FileSystemError):
        DocumentProcessingService._parse_upload_relative_path(
            "papers/../report.pdf",
            "report.pdf",
        )


def test_upload_relative_path_parser_rejects_filename_mismatch():
    from app.services.document_processing_service import DocumentProcessingService

    with pytest.raises(FileSystemError):
        DocumentProcessingService._parse_upload_relative_path(
            "papers/other.pdf",
            "report.pdf",
        )


def test_upload_relative_path_parser_rejects_absolute_path():
    from app.services.document_processing_service import DocumentProcessingService

    with pytest.raises(FileSystemError):
        DocumentProcessingService._parse_upload_relative_path(
            "/papers/report.pdf",
            "report.pdf",
        )


def test_upload_relative_path_parser_rejects_empty_segment():
    from app.services.document_processing_service import DocumentProcessingService

    with pytest.raises(FileSystemError):
        DocumentProcessingService._parse_upload_relative_path(
            "papers//report.pdf",
            "report.pdf",
        )


@pytest.mark.asyncio
async def test_upload_folder_path_creates_nested_library_folders():
    """Nested uploads create completed Library folder rows for each path segment."""
    from unittest.mock import AsyncMock, patch
    from app.core.document_status import STATUS_COMPLETED
    from app.services.document_processing_service import DocumentProcessingService

    created = []

    class FakeUnitOfWork:
        def __init__(self, project_id, immediate=False):
            self.library = SimpleNamespace(
                get_child_by_title=AsyncMock(return_value=None),
                create=AsyncMock(side_effect=self._create),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def _create(self, **kwargs):
            created.append(kwargs)
            return SimpleNamespace(id=f"folder-{len(created)}")

    svc = DocumentProcessingService()

    with patch("app.services.document_processing_service.UnitOfWork", FakeUnitOfWork):
        parent_id = await svc._ensure_upload_folder_path(
            "project-id",
            None,
            ["papers", "security"],
        )

    assert parent_id == "folder-2"
    assert [item["title"] for item in created] == ["papers", "security"]
    assert all(item["is_folder"] is True for item in created)
    assert all(item["processing_status"] == STATUS_COMPLETED for item in created)
    assert created[0]["parent_id"] is None
    assert created[1]["parent_id"] == "folder-1"


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------

def test_document_conversion_error():
    from app.core.exceptions import DocumentConversionError
    err = DocumentConversionError("/path/to/file.pdf", doc_id="abc")
    assert str(err) == "Document conversion failed: /path/to/file.pdf"
    assert err.details.get("stage") == "conversion"
    assert err.details.get("doc_id") == "abc"


def test_ai_extraction_error_non_fatal():
    from app.core.exceptions import AIExtractionError
    err = AIExtractionError(doc_id="doc1", attempts=3)
    assert "3 attempts" in str(err)
    assert err.status_code == 502


# ---------------------------------------------------------------------------
# Concurrent same-title uploads: the duplicate-title check and the insert
# share one immediate write transaction, so the loser takes the rename path.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_same_title_uploads_get_distinct_db_titles(project):
    """Two concurrent uploads of one name both succeed with distinct DB
    titles: the second upload's check sees the first's committed row and
    suffixes the title instead of racing past the check into a duplicate."""
    import asyncio

    from app.database.unit_of_work import UnitOfWork
    from app.services.document_processing_service import DocumentProcessingService

    svc = DocumentProcessingService()
    results = await asyncio.gather(
        svc.upload_files(project, [ChunkedUpload(b"one", filename="report.txt")]),
        svc.upload_files(project, [ChunkedUpload(b"two", filename="report.txt")]),
    )

    assert [err for result in results for err in result["errors"]] == []
    docs = [doc for result in results for doc in result["documents"]]
    assert len({doc["id"] for doc in docs}) == 2
    titles = sorted(doc["title"] for doc in docs)
    assert titles[0] == "report"
    assert titles[1].startswith("report_")

    async with UnitOfWork(project) as uow:
        rows = [doc for doc in await uow.library.get_all() if not doc.is_folder]
    assert sorted(doc.title for doc in rows) == titles
    assert len({doc.file_path for doc in rows}) == 2
