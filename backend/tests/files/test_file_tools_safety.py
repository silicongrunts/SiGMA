"""Safety contracts for the file tools: bounded traversal, stat-first reads.

Covers virtual-filesystem root refusal and symlink-loop termination for
glob, plus the bounded-read contracts: size caps, S_ISREG rejection, and
truncation notes.

The whole-read cap (``file_tools.MAX_TOOL_READ_BYTES``) is monkeypatched
down for the range-read tests: the contract is behaviour at the size
boundary, not the 10 MiB constant, so no test materializes a >10 MB file.
"""

import os
import time
from types import SimpleNamespace

import pytest

from app.agents.tools import file_tools
from app.agents.tools.file_tools import (
    _edit_file, _glob_search, _grep_search, _list_files,
    _read_file, _read_image, _run_bounded_search,
)
from app.agents.tools.library_tools import _copy_unique_file
from app.agents.tools.notebook_utils import (
    NotebookToolError, normalize_notebook_path, read_notebook_json,
    save_notebook_json,
)
from app.core.chat_attachments import MAX_CHAT_IMAGE_BYTES
from app.core.exceptions import FileSystemError
from app.services.chat_attachments import read_image_path_base64
from app.services.file_service import MAX_TOOL_READ_BYTES, MAX_UI_READ_BYTES, file_service


def _patch_notebook_project(monkeypatch, tmp_path):
    from app.agents.tools import notebook_utils
    monkeypatch.setattr(
        notebook_utils, "settings",
        SimpleNamespace(get_project_path=lambda pid: tmp_path),
    )
    monkeypatch.setattr(notebook_utils, "get_jupyter", lambda: None)


def _sparse_file(path, size: int):
    """Create a sparse file reporting *size* bytes with almost no disk use.

    The read caps are stat-first, so a sparse file exercises the cap without
    paying for the bytes.
    """
    with open(path, "wb") as f:
        f.truncate(size)
    return path


# ── glob: virtual-filesystem roots are refused (regression) ─────────

@pytest.mark.regression
@pytest.mark.asyncio
async def test_glob_refuses_root_path(sandbox):
    result = await _glob_search("proj", "**/*.txt", "/")
    assert result.startswith("Error:")
    assert "refusing to search" in result
    # The refusal suggests the concrete project directory instead.
    assert str(sandbox) in result


@pytest.mark.regression
@pytest.mark.asyncio
async def test_glob_refuses_proc_path(sandbox):
    result = await _glob_search("proj", "*", "/proc")
    assert result.startswith("Error:")
    assert "refusing to search" in result


@pytest.mark.regression
@pytest.mark.asyncio
async def test_glob_refuses_proc_absolute_pattern(sandbox):
    result = await _glob_search("proj", "/proc/**/environ")
    assert result.startswith("Error:")
    assert "refusing to search" in result


@pytest.mark.asyncio
async def test_glob_absolute_pattern_on_real_dir(sandbox):
    (sandbox / "a.txt").write_text("a")
    result = await _glob_search("proj", str(sandbox / "**" / "*.txt"))
    assert str(sandbox / "a.txt") in result


# ── grep: virtual-filesystem roots are refused too ──────────────────

@pytest.mark.regression
@pytest.mark.asyncio
async def test_grep_refuses_proc_path(sandbox):
    result = await _grep_search("proj", "needle", "/proc")
    assert result.startswith("Error:")
    assert "refusing to search" in result


@pytest.mark.asyncio
async def test_grep_single_file_in_virtual_tree_still_allowed():
    """A single named file is a bounded read, not a tree walk — allowed."""
    result = await _grep_search("proj", "needle", "/proc/cpuinfo")
    assert "refusing to search" not in result


# ── glob: symlink loops must terminate ──────────────────────────────

def _symlink_loop_tree(tmp_path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("b")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "h.txt").write_text("h")
    # Self-referential directory symlink: a walker that follows directory
    # symlinks never terminates.
    (tmp_path / "loop").symlink_to(tmp_path)


@pytest.mark.regression
@pytest.mark.asyncio
async def test_glob_symlink_loop_terminates_rg_path(sandbox):
    _symlink_loop_tree(sandbox)

    result = await _glob_search("proj", "**/*.txt", ".")
    lines = result.split("\n")
    assert "a.txt" in lines
    assert "sub/b.txt" in lines
    assert ".hidden/h.txt" not in result


@pytest.mark.regression
@pytest.mark.asyncio
async def test_glob_symlink_loop_terminates_fallback_walk(sandbox, monkeypatch):
    _symlink_loop_tree(sandbox)

    async def _rg_missing(*args, **kwargs):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(file_tools, "_glob_via_rg", _rg_missing)
    result = await _glob_search("proj", "**/*.txt", ".")
    lines = result.split("\n")
    assert "a.txt" in lines
    assert "sub/b.txt" in lines
    assert ".hidden/h.txt" not in result


@pytest.mark.regression
@pytest.mark.asyncio
async def test_glob_fallback_trailing_doublestar_filters_hidden(
        sandbox, monkeypatch):
    """A trailing '**' in the fallback walker applies the same hidden-entry
    rule as every other branch (rg parity)."""
    _symlink_loop_tree(sandbox)

    async def _rg_missing(*args, **kwargs):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(file_tools, "_glob_via_rg", _rg_missing)
    result = await _glob_search("proj", "**", ".")
    assert ".hidden/h.txt" not in result
    assert "a.txt" in result.split("\n")
    assert "sub/b.txt" in result.split("\n")


# ── bounded-search engine ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_bounded_search_timeout_kills_promptly():
    start = time.monotonic()
    run = await _run_bounded_search(
        ["sleep", "30"], cwd=None, timeout=0.3)
    elapsed = time.monotonic() - start
    assert run.bound_exceeded is True
    assert run.stdout == b""
    # The kill must not wait out the command's own runtime.
    assert elapsed < 3.0


@pytest.mark.asyncio
async def test_run_bounded_search_output_cap(monkeypatch):
    monkeypatch.setattr(file_tools, "_SEARCH_MAX_OUTPUT_BYTES", 100_000)
    run = await _run_bounded_search(
        ["bash", "-c", "printf 'a%.0s' {1..200000}"],
        cwd=None, timeout=30)
    assert run.bound_exceeded is True
    assert 100_000 <= len(run.stdout) <= 100_000 + 2 * 65536


@pytest.mark.regression
@pytest.mark.asyncio
async def test_glob_budget_exceeded_returns_error(sandbox, monkeypatch):
    (sandbox / "a.txt").write_text("a")
    monkeypatch.setattr(file_tools, "_GLOB_TIMEOUT_SECONDS", 0.001)

    result = await _glob_search("proj", "*.txt", ".")
    assert result.startswith("Error:")
    assert "search budget" in result
    assert "more specific" in result


@pytest.mark.asyncio
async def test_glob_budget_exceeded_with_partial_results_notes(sandbox, monkeypatch):
    async def _partial(*args, **kwargs):
        return (["a.txt"], True, False)

    monkeypatch.setattr(file_tools, "_glob_via_rg", _partial)
    result = await _glob_search("proj", "*.txt", ".")
    assert "a.txt" in result
    assert "search stopped early" in result


@pytest.mark.regression
@pytest.mark.asyncio
async def test_grep_timeout_returns_error_and_kills(sandbox, monkeypatch):
    """The bounded engine stops its subprocess at the deadline and reports
    the timeout; nothing is left running."""
    monkeypatch.setattr(file_tools, "_GREP_TIMEOUT_SECONDS", 0.001)
    _hand_engine_a_stuck_subprocess(monkeypatch)

    start = time.monotonic()
    result = await _grep_search("proj", "needle", ".")
    elapsed = time.monotonic() - start
    assert "timed out" in result
    # Generous bound: it must catch a hung engine (minutes) without tripping
    # on scheduler stalls on a loaded machine.
    assert elapsed < 30.0


def _hand_engine_a_stuck_subprocess(monkeypatch):
    """Run the real bounded engine against a subprocess that cannot finish.

    grep/rg on a small sandbox tree can complete in well under a 1 ms budget
    on a fast machine, which would turn the timeout branch into a
    machine-speed race. Feeding the engine ``sleep 30`` keeps the contract
    under test — deadline exceeded, subprocess SIGKILLed, prompt return —
    deterministic.
    """
    real_bounded = file_tools._run_bounded_search

    async def _stuck(cmd, **kwargs):
        return await real_bounded(["sleep", "30"], **kwargs)

    monkeypatch.setattr(file_tools, "_run_bounded_search", _stuck)


@pytest.mark.regression
@pytest.mark.asyncio
async def test_grep_fallback_timeout_kills_and_reports(
        sandbox, monkeypatch):
    """The grep fallback shares the bounded engine: a timeout kills the
    process (no orphaned subprocess) and returns within the bound, not after
    the command's own runtime."""
    (sandbox / "a.txt").write_text("needle")

    async def _rg_missing(cmd, *, cwd, timeout):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(file_tools, "_run_rg_search", _rg_missing)
    monkeypatch.setattr(file_tools, "_GREP_TIMEOUT_SECONDS", 0.001)
    _hand_engine_a_stuck_subprocess(monkeypatch)

    start = time.monotonic()
    result = await _grep_search("proj", "needle", ".")
    elapsed = time.monotonic() - start
    assert "timed out" in result
    # Generous bound: it must catch a hung engine (minutes) without tripping
    # on scheduler stalls on a loaded machine.
    assert elapsed < 30.0


@pytest.mark.asyncio
async def test_grep_fallback_timeout_message_from_engine(monkeypatch):
    """Fallback outcome mapping: engine timeout with no output → timeout
    error; engine timeout with output → results plus stopped-early note."""

    async def _run(cmd, *, cwd, timeout):
        return file_tools._SearchRun(b"", b"", None, True)

    monkeypatch.setattr(file_tools, "_run_bounded_search", _run)
    result = await file_tools._grep_fallback(
        "needle", ".", None, "",
        output_mode="content", case_insensitive=False, context=0,
        after_context=0, before_context=0, multiline=False, type_filter="",
        head_limit=10, offset=0)
    assert result == "grep error: search timed out after 15s"

    async def _run_partial(cmd, *, cwd, timeout):
        # Two lines: the last may be truncated by the mid-stream kill, so
        # only the first survives the drop-partial-line rule.
        return file_tools._SearchRun(
            b"a.txt:1:needle\nb.txt:2:needle\n", b"", 0, True)

    monkeypatch.setattr(file_tools, "_run_bounded_search", _run_partial)
    result = await file_tools._grep_fallback(
        "needle", ".", None, "",
        output_mode="content", case_insensitive=False, context=0,
        after_context=0, before_context=0, multiline=False, type_filter="",
        head_limit=10, offset=0)
    assert "a.txt:1:needle" in result
    assert "search stopped early" in result


# ── read caps: stat-first, S_ISREG, size limits ─────────────────────

@pytest.mark.regression
@pytest.mark.asyncio
async def test_read_absolute_oversize_rejected_stat_first(tmp_path):
    big = _sparse_file(tmp_path / "big.txt", MAX_TOOL_READ_BYTES + 1)
    with pytest.raises(FileSystemError) as exc_info:
        await file_service.read_file_absolute(str(big), max_bytes=MAX_TOOL_READ_BYTES)
    assert exc_info.value.code == "FILE_TOO_LARGE"
    assert "too large" in str(exc_info.value)


@pytest.mark.regression
@pytest.mark.asyncio
async def test_read_fifo_rejected_by_regular_file_check(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    # A pre-stat read would block forever on the fifo; the S_ISREG check
    # must reject it before any byte is read.
    with pytest.raises(FileSystemError) as exc_info:
        await file_service.read_file_absolute(str(fifo), max_bytes=None)
    assert exc_info.value.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_read_sandbox_oversize_rejected(sandbox):
    _sparse_file(sandbox / "big.txt", MAX_TOOL_READ_BYTES + 1)
    with pytest.raises(FileSystemError) as exc_info:
        await file_service.read_file("proj", "big.txt", max_bytes=MAX_TOOL_READ_BYTES)
    assert exc_info.value.code == "FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_read_ui_cap_rejects_oversize_text(sandbox):
    """The UI content route's 5 MiB cap rejects oversized text before any
    byte is read — the editor degrades to a download panel instead of
    loading a tab-freezing document."""
    _sparse_file(sandbox / "huge.json", MAX_UI_READ_BYTES + 1)
    with pytest.raises(FileSystemError) as exc_info:
        await file_service.read_file("proj", "huge.json", max_bytes=MAX_UI_READ_BYTES)
    assert exc_info.value.code == "FILE_TOO_LARGE"
    assert "too large to read whole" in str(exc_info.value)


@pytest.mark.asyncio
async def test_read_ui_cap_boundary_exact_limit_reads(sandbox):
    """A file exactly at the cap is readable — the limit is exclusive."""
    (sandbox / "edge.txt").write_text("a" * MAX_UI_READ_BYTES, encoding="utf-8")
    text = await file_service.read_file("proj", "edge.txt", max_bytes=MAX_UI_READ_BYTES)
    assert len(text) == MAX_UI_READ_BYTES


@pytest.mark.asyncio
async def test_read_file_tool_big_text_file_reads_window(
        tmp_path, monkeypatch):
    """Files over the whole-read cap stay readable via the streaming range
    reader — only the requested window is accumulated."""
    monkeypatch.setattr(file_tools, "MAX_TOOL_READ_BYTES", 16 * 1024)
    # ~45 KB (> the patched cap) so the streaming range reader is used.
    big = tmp_path / "big.log"
    big.write_text("\n".join(
        f"line-{i:07d} {'x' * 32}" for i in range(1000)))
    assert big.stat().st_size > 16 * 1024

    result = await _read_file("proj", "sess", str(big))
    assert not result.startswith("Error")
    assert result.split("\n")[0].startswith("1\tline-0000000")
    assert "Showing lines 1-200 of 1000" in result

    # A window in the middle keeps absolute line numbers.
    middle = await _read_file("proj", "sess", str(big), offset=990, limit=5)
    assert "991\tline-0000990" in middle
    assert "995\tline-0000994" in middle

    # Negative limit returns the last lines of a big file, and nothing else.
    tail = await _read_file("proj", "sess", str(big), limit=-3)
    assert "998\tline-0000997" in tail
    assert "line-0000990" not in tail


@pytest.mark.asyncio
async def test_read_big_utf16_file_reads_window(tmp_path, monkeypatch):
    """BOM'd UTF-16 files over the whole-read cap stay window-readable: the
    range reader splits on the 2-byte newline unit of the file's byte order
    instead of cutting between code units."""
    monkeypatch.setattr(file_tools, "MAX_TOOL_READ_BYTES", 16 * 1024)
    big = tmp_path / "big-u16.log"
    with open(big, "wb") as f:
        f.write("\n".join(
            f"line-{i:07d} {'x' * 32}" for i in range(1000)
        ).encode("utf-16"))  # codec prepends the LE BOM
    assert big.stat().st_size > 16 * 1024

    result = await _read_file("proj", "sess", str(big))
    assert not result.startswith("Error")
    assert result.split("\n")[0].startswith("1\tline-0000000")
    assert "Showing lines 1-200 of 1000" in result

    middle = await _read_file("proj", "sess", str(big), offset=990, limit=2)
    assert "991\tline-0000990" in middle

    tail = await _read_file("proj", "sess", str(big), limit=-2)
    assert "1000\tline-0000999" in tail


@pytest.mark.asyncio
async def test_read_file_tool_oversize_binary_sparse(tmp_path):
    """A >10 MB hole-filled file routes to the range reader, whose header
    binary check rejects NUL-filled content."""
    big = _sparse_file(tmp_path / "big.log", MAX_TOOL_READ_BYTES + 1)
    result = await _read_file("proj", "sess", str(big))
    assert result.startswith("Error:")
    assert "binary" in result.lower()


@pytest.mark.asyncio
async def test_read_file_tool_fifo_absolute(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    result = await _read_file("proj", "sess", str(fifo))
    assert result.startswith("Error:")
    assert "Not a regular file" in result


@pytest.mark.asyncio
async def test_read_trailing_newline_counts_no_phantom_line(tmp_path):
    """A trailing newline terminates the last line; the read output must not
    show a phantom extra empty line (parity with the streaming range reader
    across the MAX_TOOL_READ_BYTES boundary)."""
    p = tmp_path / "nl.txt"
    p.write_text("a\nb\n")
    result = await _read_file("proj", "sess", str(p))
    lines = result.split("\n")
    assert lines[0] == "1\ta"
    assert lines[1] == "2\tb"
    assert len(lines) == 2  # no "3\t" phantom, no truncation footer


@pytest.mark.asyncio
async def test_edit_oversize_clean_error(tmp_path, monkeypatch):
    big = _sparse_file(tmp_path / "big.log", MAX_TOOL_READ_BYTES + 1)
    # Bypass must-read-first so the read-cap contract itself is exercised.
    monkeypatch.setattr(file_tools, "_must_read_first_error_for", lambda *a, **k: None)
    result = await _edit_file("proj", "sess", str(big), "a", "b")
    assert result.startswith("Error:")
    assert "too large" in result
    assert "sed -i" in result


# ── list_files truncation ────────────────────────────────────────────

def _many_files(directory, count):
    for i in range(count):
        (directory / f"f{i:05d}.txt").write_text("x")


@pytest.mark.asyncio
async def test_list_files_truncates_large_directory(tmp_path):
    _many_files(tmp_path, file_tools._MAX_LIST_ENTRIES + 5)
    (tmp_path / ".hiddenfile").write_text("x")

    result = await _list_files("proj", str(tmp_path))
    lines = result.split("\n")
    assert len(lines) == file_tools._MAX_LIST_ENTRIES + 1
    assert lines[-1].startswith("... +5 more entries not shown")
    assert all(not l.startswith(".") for l in lines[:-1])


@pytest.mark.asyncio
async def test_list_files_truncates_large_sandbox_directory(sandbox):
    sub = sandbox / "sub"
    sub.mkdir()
    _many_files(sub, file_tools._MAX_LIST_ENTRIES + 5)

    result = await _list_files("proj", "sub")
    lines = result.split("\n")
    assert len(lines) == file_tools._MAX_LIST_ENTRIES + 1
    assert lines[-1].startswith("... +5 more entries not shown")
    assert lines[0] == "f00000.txt"


@pytest.mark.asyncio
async def test_list_files_truncates_large_project_tree(sandbox):
    _many_files(sandbox, file_tools._MAX_LIST_ENTRIES + 5)

    result = await _list_files("proj", "")
    lines = result.split("\n")
    assert len(lines) == file_tools._MAX_LIST_ENTRIES + 1
    assert lines[-1].startswith("... +5 more entries not shown")


# ── image reads: stat-first byte cap ─────────────────────────────────

@pytest.mark.regression
@pytest.mark.asyncio
async def test_read_image_tool_oversize_rejected_before_read(tmp_path):
    big_png = _sparse_file(tmp_path / "big.png", MAX_CHAT_IMAGE_BYTES + 1)
    result = await _read_image("proj", "sess", str(big_png), ".png")
    assert result.startswith("Error:")
    assert "too large" in result


@pytest.mark.regression
@pytest.mark.asyncio
async def test_chat_attachment_image_oversize_rejected_before_read(tmp_path):
    big_png = _sparse_file(tmp_path / "big.png", MAX_CHAT_IMAGE_BYTES + 1)
    with pytest.raises(FileSystemError) as exc_info:
        await read_image_path_base64("proj", str(big_png))
    assert exc_info.value.code == "FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_chat_attachment_image_fifo_rejected(tmp_path):
    fifo = tmp_path / "pipe.png"
    os.mkfifo(fifo)
    with pytest.raises(FileSystemError) as exc_info:
        await read_image_path_base64("proj", str(fifo))
    assert exc_info.value.code == "INVALID_REQUEST"


# ── notebook: bounded fallback read/write ────────────────────────────

@pytest.mark.asyncio
async def test_notebook_round_trip_filesystem_fallback(tmp_path, monkeypatch):
    _patch_notebook_project(monkeypatch, tmp_path)
    notebook = {
        "cells": [{"id": "c1", "cell_type": "code", "source": "print(1)",
                   "metadata": {}, "outputs": [], "execution_count": None}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }
    location = normalize_notebook_path("nb.ipynb", "proj")
    assert await save_notebook_json(location, notebook) is True

    loaded, _ = await read_notebook_json("nb.ipynb", "proj")
    assert loaded == notebook


@pytest.mark.regression
@pytest.mark.asyncio
async def test_notebook_oversize_rejected_stat_first(tmp_path, monkeypatch):
    _patch_notebook_project(monkeypatch, tmp_path)
    from app.agents.tools import notebook_utils
    _sparse_file(tmp_path / "nb.ipynb", notebook_utils._MAX_NOTEBOOK_BYTES + 1)
    with pytest.raises(NotebookToolError) as exc_info:
        await read_notebook_json("nb.ipynb", "proj")
    assert "too large" in str(exc_info.value)


@pytest.mark.asyncio
async def test_notebook_fifo_rejected(tmp_path, monkeypatch):
    _patch_notebook_project(monkeypatch, tmp_path)
    os.mkfifo(tmp_path / "nb.ipynb")
    with pytest.raises(NotebookToolError) as exc_info:
        await read_notebook_json("nb.ipynb", "proj")
    assert "regular file" in str(exc_info.value)


# ── library import copy ──────────────────────────────────────────────

def test_copy_unique_file_copies_and_dedups(tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4 stub")
    library_dir = tmp_path / "library"
    library_dir.mkdir()

    first = _copy_unique_file(src, library_dir / "src.pdf")
    second = _copy_unique_file(src, library_dir / "src.pdf")

    assert first == library_dir / "src.pdf"
    assert second == library_dir / "src_1.pdf"
    assert second.read_bytes() == src.read_bytes()
