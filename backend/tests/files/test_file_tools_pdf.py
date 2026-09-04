"""Tests for PDF reading in the read tool.

PDF conversion via docling is slow, so these tests mock the DocumentConverter
to return canned markdown. They verify the cache + slicing integration
rather than docling itself.
"""

import sys

import pytest

from app.agents.tools.file_tools import _read_file
from app.agents.tools.read_state import read_state_cache


def _mock_docling(monkeypatch, markdown: str, call_counter: list | None = None):
    """Patch docling.DocumentConverter so .convert().document.export_to_markdown()
    returns *markdown*. Records the number of conversions in *call_counter*."""
    class _FakeDoc:
        def export_to_markdown(self) -> str:
            return markdown

    class _FakeResult:
        document = _FakeDoc()

    class _FakeConverter:
        def __init__(self, *args, **kwargs):
            pass
        def convert(self, path):
            if call_counter is not None:
                call_counter.append(1)
            return _FakeResult()

    # Inject into sys.modules so the function-local import picks it up
    fake_module = type(sys)("docling")
    fake_module.document_converter = type(sys)("docling.document_converter")
    fake_module.document_converter.DocumentConverter = _FakeConverter
    monkeypatch.setitem(sys.modules, "docling", fake_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_module.document_converter)


@pytest.mark.asyncio
async def test_read_pdf_returns_converted_markdown(sandbox, monkeypatch):
    pdf = sandbox / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake but exists so file_service.read_file_binary succeeds")

    _mock_docling(monkeypatch, "# Title\n\nPara one.\n")

    result = await _read_file("proj", "sess", "doc.pdf")
    assert isinstance(result, str)
    assert "Title" in result
    assert "Para one" in result


@pytest.mark.asyncio
async def test_read_pdf_caches_conversion(sandbox, monkeypatch):
    """The second read of the same PDF reuses the cached markdown and does
    not invoke docling again (as long as mtime is unchanged)."""
    pdf = sandbox / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")

    call_counter: list = []
    _mock_docling(monkeypatch, "cached markdown content\n", call_counter)

    await _read_file("proj", "sess", "doc.pdf")
    await _read_file("proj", "sess", "doc.pdf")

    assert len(call_counter) == 1  # conversion ran only once


@pytest.mark.asyncio
async def test_read_pdf_offset_limit_slices_markdown(sandbox, monkeypatch):
    pdf = sandbox / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")

    md_lines = [f"line {i}" for i in range(20)]
    _mock_docling(monkeypatch, "\n".join(md_lines))

    result = await _read_file("proj", "sess", "doc.pdf", offset=5, limit=3)
    # offset=5 → 0-indexed line 5 (i.e. "line 5"); limit=3 → 3 lines
    assert "line 5" in result
    assert "line 7" in result
    assert "line 4" not in result
    assert "line 8" not in result


@pytest.mark.asyncio
async def test_read_pdf_missing_file_returns_error(sandbox):
    result = await _read_file("proj", "sess", "missing.pdf")
    assert isinstance(result, str)
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_read_pdf_partial_read_satisfies_must_read_first(sandbox, monkeypatch):
    """A paginated read (offset/limit) satisfies must-read-first."""
    pdf = sandbox / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")

    _mock_docling(monkeypatch, "\n".join(f"line {i}" for i in range(20)))

    await _read_file("proj", "sess", "doc.pdf", offset=0, limit=5)
    assert read_state_cache.get("sess", str(pdf)) is not None


@pytest.mark.asyncio
async def test_read_pdf_full_read_satisfies_must_read_first(sandbox, monkeypatch):
    pdf = sandbox / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 body")

    _mock_docling(monkeypatch, "single page of content")

    await _read_file("proj", "sess", "doc.pdf")
    assert read_state_cache.get("sess", str(pdf)) is not None
