"""End-to-end tests for the must-read-first contract.

Covers the integration between the ``read_state_cache`` and the ``write`` /
``edit`` tools: a write or edit must succeed only if the target file has been
read in the same session, and must fail after a compaction (cache clear) or if
the file has been modified on disk since the read.
"""

import os
import time

import pytest

from app.agents.tools.file_tools import _read_file, _write_file, _edit_file
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


# ── write ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_new_file_does_not_require_prior_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    result = await _write_file("proj", "sess", "new.txt", "hello")
    assert result.startswith("File written:")
    assert (tmp_path / "new.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_write_existing_file_requires_prior_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "exists.txt").write_text("old")

    result = await _write_file("proj", "sess", "exists.txt", "new")
    assert result.startswith("Error:")
    assert "has not been read" in result


@pytest.mark.asyncio
async def test_write_succeeds_after_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "exists.txt").write_text("old")

    await _read_file("proj", "sess", "exists.txt")
    result = await _write_file("proj", "sess", "exists.txt", "new")
    assert result.startswith("File written:")
    assert (tmp_path / "exists.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_write_succeeds_after_partial_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "multi.txt").write_text("\n".join(str(i) for i in range(50)))

    # Paginated reads satisfy the must-read-first contract.
    await _read_file("proj", "sess", "multi.txt", offset=0, limit=5)
    result = await _write_file("proj", "sess", "multi.txt", "overwritten")
    assert result.startswith("File written:")
    assert (tmp_path / "multi.txt").read_text() == "overwritten"


@pytest.mark.asyncio
async def test_write_succeeds_with_equivalent_absolute_path(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "same.txt"
    target.write_text("old")

    await _read_file("proj", "sess", "same.txt")
    result = await _write_file("proj", "sess", str(target), "new")
    assert result.startswith("File written:")
    assert target.read_text() == "new"


@pytest.mark.asyncio
async def test_write_fails_after_compaction_clears_cache(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "f.txt").write_text("v1")

    await _read_file("proj", "sess", "f.txt")
    # Simulate compaction
    read_state_cache.clear("sess")
    result = await _write_file("proj", "sess", "f.txt", "v2")
    assert result.startswith("Error:")
    assert "has not been read" in result


@pytest.mark.asyncio
async def test_write_fails_when_file_modified_since_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("v1")

    await _read_file("proj", "sess", "f.txt")

    # Bump mtime forward to simulate external modification
    target.write_text("external-change")
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 5))

    result = await _write_file("proj", "sess", "f.txt", "v2")
    assert result.startswith("Error:")


# ── edit ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_edit_requires_prior_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "e.txt").write_text("hello world")

    result = await _edit_file(
        "proj", "sess", "e.txt", "hello", "goodbye",
    )
    assert result.startswith("Error:")
    assert "has not been read" in result


@pytest.mark.asyncio
async def test_edit_succeeds_after_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "e.txt").write_text("hello world")

    await _read_file("proj", "sess", "e.txt")
    result = await _edit_file("proj", "sess", "e.txt", "hello", "goodbye")
    assert result.startswith("File edited:")
    assert (tmp_path / "e.txt").read_text() == "goodbye world"


@pytest.mark.asyncio
async def test_edit_succeeds_after_partial_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "e.txt").write_text("hello world")

    await _read_file("proj", "sess", "e.txt", offset=0, limit=1)
    result = await _edit_file("proj", "sess", "e.txt", "hello", "goodbye")
    assert result.startswith("File edited:")
    assert (tmp_path / "e.txt").read_text() == "goodbye world"


@pytest.mark.asyncio
async def test_edit_succeeds_with_equivalent_dot_relative_path(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "e.txt"
    target.write_text("hello world")

    await _read_file("proj", "sess", str(target))
    result = await _edit_file("proj", "sess", "./e.txt", "hello", "goodbye")
    assert result.startswith("File edited:")
    assert target.read_text() == "goodbye world"


@pytest.mark.asyncio
async def test_edit_identical_strings_rejected(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "e.txt").write_text("hello")

    await _read_file("proj", "sess", "e.txt")
    result = await _edit_file("proj", "sess", "e.txt", "hello", "hello")
    assert result.startswith("Error:")
    assert "identical" in result


@pytest.mark.asyncio
async def test_edit_non_unique_old_string_rejected(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "e.txt").write_text("dup dup")

    await _read_file("proj", "sess", "e.txt")
    result = await _edit_file("proj", "sess", "e.txt", "dup", "one", replace_all=False)
    assert "appears 2 times" in result


@pytest.mark.asyncio
async def test_edit_replace_all(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "e.txt").write_text("dup dup")

    await _read_file("proj", "sess", "e.txt")
    result = await _edit_file("proj", "sess", "e.txt", "dup", "x", replace_all=True)
    assert result.startswith("File edited:")
    assert (tmp_path / "e.txt").read_text() == "x x"


# ── cross-tool: read then edit then write ────────────────────────────

@pytest.mark.asyncio
async def test_edit_refreshes_cache_allowing_subsequent_write(tmp_path, monkeypatch):
    """After a successful edit, the cache is refreshed — a same-turn write
    does not require another read."""
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "e.txt").write_text("hello world")

    await _read_file("proj", "sess", "e.txt")
    await _edit_file("proj", "sess", "e.txt", "hello", "goodbye")
    result = await _write_file("proj", "sess", "e.txt", "fresh content")
    assert result.startswith("File written:")
