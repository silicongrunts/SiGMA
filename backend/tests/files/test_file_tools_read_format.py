"""Tests for the read tool's output formatting.

Covers the `cat -n` line-number prefix (real 1-indexed file line numbers,
including in paginated reads) and the standardized `Showing lines X-Y of Z`
footer that must appear whenever the returned window stops short of EOF —
including the explicit-`limit` case that previously produced no footer.
"""

import pytest

from app.agents.tools.file_tools import _read_file
from app.agents.tools.read_state import read_state_cache


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """Ensure each test starts with an empty cache."""
    read_state_cache.clear("sess")
    yield
    read_state_cache.clear("sess")


def _patch_file_service(monkeypatch, tmp_path):
    """Point file_service at a sandbox rooted at tmp_path."""
    from app.services.file_service import file_service
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)


@pytest.mark.asyncio
async def test_read_prepends_cat_n_line_numbers(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "f.txt").write_text("alpha\nbeta\ngamma")

    result = await _read_file("proj", "sess", "f.txt")

    lines = result.split("\n")
    assert lines[0] == "1\talpha"
    assert lines[1] == "2\tbeta"
    assert lines[2] == "3\tgamma"
    # Short file → no truncation footer.
    assert "Showing lines" not in result


@pytest.mark.asyncio
async def test_read_default_cap_emits_footer(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "big.txt").write_text("\n".join(f"l{i}" for i in range(250)))

    result = await _read_file("proj", "sess", "big.txt")

    assert "Showing lines 1-200 of 250" in result
    assert "50 more not shown" in result
    # First line carries the real file line number 1.
    assert result.split("\n")[0] == "1\tl0"
    # Line 200 (0-indexed 199) is the last shown.
    assert "200\tl199" in result


@pytest.mark.asyncio
async def test_read_explicit_limit_truncation_emits_footer(tmp_path, monkeypatch):
    """Regression: an explicit limit that truncates must also emit the footer.

    Previously the footer was gated on `defaulted`, so explicit-limit reads
    produced no signal that more lines remained — exactly the pagination
    scenario where the signal matters most.
    """
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "big.txt").write_text("\n".join(f"l{i}" for i in range(100)))

    result = await _read_file("proj", "sess", "big.txt", offset=10, limit=30)

    # Window: lines 11..40 (1-indexed), 60 more hidden.
    assert "Showing lines 11-40 of 100" in result
    assert "60 more not shown" in result
    assert result.split("\n")[0] == "11\tl10"


@pytest.mark.asyncio
async def test_read_offset_window_line_numbers_are_absolute(tmp_path, monkeypatch):
    """Paginated reads expose absolute file line numbers, not window-relative.

    This is what makes the output safe for `sigma://...&line=N` citations: the
    first returned line of an offset read already shows the real file line.
    """
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "f.txt").write_text("\n".join(f"l{i}" for i in range(10)))

    result = await _read_file("proj", "sess", "f.txt", offset=5, limit=3)

    body = result.split("\n")
    assert body[0] == "6\tl5"
    assert body[1] == "7\tl6"
    assert body[2] == "8\tl7"
    assert "Showing lines 6-8 of 10" in result
    assert "2 more not shown" in result


@pytest.mark.asyncio
async def test_read_negative_limit_returns_tail_with_real_line_numbers(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "f.txt").write_text("\n".join(f"l{i}" for i in range(10)))

    result = await _read_file("proj", "sess", "f.txt", limit=-3)

    body = result.split("\n")
    # Last 3 lines of a 10-line file: lines 8, 9, 10.
    assert body[0] == "8\tl7"
    assert body[1] == "9\tl8"
    assert body[2] == "10\tl9"
    # Tail read reaches EOF → no footer.
    assert "Showing lines" not in result


@pytest.mark.asyncio
async def test_read_no_footer_when_file_fits(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "f.txt").write_text("only\nfour\nlines\nhere")

    result = await _read_file("proj", "sess", "f.txt")

    assert "Showing lines" not in result


@pytest.mark.asyncio
async def test_read_empty_file_returns_empty_string(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "empty.txt").write_text("")

    result = await _read_file("proj", "sess", "empty.txt")

    # Empty content splits to a single empty line; no line-number prefix, no footer.
    assert result == ""
