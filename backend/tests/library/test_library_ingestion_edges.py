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
# Text file encoding
# ---------------------------------------------------------------------------

def test_read_text_with_invalid_utf8():
    """Text files with invalid UTF-8 should be readable with errors='replace'."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="wb") as f:
        # Invalid UTF-8 sequence
        f.write(b"Hello \xff\xfe World")
        tmp_path = f.name

    try:
        content = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
        assert "Hello" in content
        assert "World" in content
        # Replacement chars should appear
        assert "\ufffd" in content
    finally:
        os.unlink(tmp_path)


def test_read_empty_text_file():
    """Empty text files should return empty string."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        tmp_path = f.name

    try:
        content = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
        assert content == ""
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
        def __init__(self, project_id):
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
    assert "conversion" in str(err).lower() or "file.pdf" in str(err)
    assert err.details.get("stage") == "conversion"
    assert err.details.get("doc_id") == "abc"


def test_ai_extraction_error_non_fatal():
    from app.core.exceptions import AIExtractionError
    err = AIExtractionError(doc_id="doc1", attempts=3)
    assert "3 attempts" in str(err)
    assert err.status_code == 502
