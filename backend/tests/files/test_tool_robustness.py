"""Robustness contracts for the file/search tools.

- Search error classification: bad regex/path/resource errors must surface as
  errors, never as a silent "No matches".
- EAGAIN retry in single-threaded mode.
- CR stripping and --max-columns in grep output.
- Streaming range reads: files over the whole-read cap stay windowed-readable,
  with a bounded scan and degraded totals.
- Edit robustness: CRLF/UTF-16/BOM round-trips, read-window region gate,
  notebook guard.
- Image downscaling when Pillow is available.
- Task cancellation reaches in-flight tools and kills their subprocesses.
"""

import asyncio
import base64
import io
import time
import os
from types import SimpleNamespace

import pytest

from app.agents.tools import file_tools
from app.agents.tools.file_tools import (
    _edit_file, _edit_preflight, _glob_search, _grep_search,
    _read_file, _read_image, _run_bounded_search, _SearchRun,
)
from app.agents.tools.read_state import read_state_cache
from app.services import file_service as file_service_module
from app.services.file_service import MAX_TOOL_READ_BYTES, file_service


@pytest.fixture(autouse=True)
def _clear_read_state():
    read_state_cache.clear("sess")
    yield
    read_state_cache.clear("sess")


def _patch_file_service(monkeypatch, tmp_path):
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)


def _big_text_file(tmp_path, lines=300_000, width=32):
    """A real >10 MB text file (>MAX_TOOL_READ_BYTES) for range reads."""
    path = tmp_path / "big.log"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(f"line-{i:07d} {'x' * width}" for i in range(lines)))
    assert path.stat().st_size > MAX_TOOL_READ_BYTES
    return path


# ── A: search failures are errors, not false "no matches" ────────────

@pytest.mark.asyncio
async def test_glob_nonexistent_directory_is_an_error(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    result = await _glob_search("proj", "*.py", "no_such_dir")
    assert result.startswith("Error:")
    assert "does not exist" in result


@pytest.mark.asyncio
async def test_glob_invalid_pattern_is_an_error(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("a")
    result = await _glob_search("proj", "[a", ".")
    assert result.startswith("Error:")
    assert "glob search failed" in result
    assert "unclosed character class" in result


@pytest.mark.asyncio
async def test_grep_nonexistent_path_is_an_error(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    result = await _grep_search("proj", "hello", "missing_dir")
    assert result.startswith("Error:")
    assert "does not exist" in result


@pytest.mark.asyncio
async def test_grep_invalid_regex_is_an_error(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    result = await _grep_search("proj", "[", ".")
    assert result.startswith("grep error")
    assert "unclosed character class" in result
    assert "No matches" not in result


@pytest.mark.asyncio
async def test_grep_invalid_regex_via_fallback_is_an_error(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("hello")

    async def _rg_missing(cmd, *, cwd, timeout):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(file_tools, "_run_rg_search", _rg_missing)
    # `[` is invalid in both rg's regex and GNU grep's BRE.
    result = await _grep_search("proj", "[", ".")
    assert result.startswith("grep error")
    assert "No matches" not in result


@pytest.mark.asyncio
async def test_grep_valid_no_match_still_says_no_matches(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("hello")
    result = await _grep_search("proj", "zzzz", ".")
    assert result == "No matches for 'zzzz'"


# ── E: EAGAIN retry, env timeout, CR strip, --max-columns ────────────

@pytest.mark.asyncio
async def test_grep_eagain_retries_single_threaded(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("needle")

    calls = []

    async def _fake_bounded(cmd, *, cwd, timeout):
        calls.append(list(cmd))
        if len(calls) == 1:
            # Thread-pool spawn failure in ulimited containers.
            return _SearchRun(b"", b"rg: os error 11 (Resource temporarily "
                              b"unavailable) while spawning threads", 2, False)
        return _SearchRun(b"a.txt\n", b"", 0, False)

    monkeypatch.setattr(file_tools, "_run_bounded_search", _fake_bounded)
    result = await _grep_search("proj", "needle", ".")
    assert "a.txt" in result
    assert len(calls) == 2
    assert calls[1][1:3] == ["-j", "1"]


@pytest.mark.asyncio
async def test_grep_strips_trailing_cr(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "crlf.txt").write_bytes(b"needle here\r\nplain\n")
    result = await _grep_search(
        "proj", "needle", ".", output_mode="content")
    assert "\r" not in result
    assert "needle here" in result


@pytest.mark.asyncio
async def test_grep_omits_long_lines(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "min.txt").write_text("short\n" + "y" * 2000 + "\n")
    result = await _grep_search(
        "proj", "y", ".", glob_filter="min.txt", output_mode="content")
    assert "[Omitted long matching line]" in result
    assert "short" not in result or "yy" not in result


# ── B: streaming range reads of large files ──────────────────────────

@pytest.mark.asyncio
async def test_read_big_file_degraded_footer_when_scan_budget_hit(
        tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    big = _big_text_file(tmp_path)
    monkeypatch.setattr(file_service_module, "_RANGE_SCAN_MAX_BYTES",
                        2 * 1024 * 1024)

    result = await _read_file("proj", "sess", str(big))
    assert not result.startswith("Error")
    assert "too large to count the remaining lines" in result
    assert result.split("\n")[0].startswith("1\tline-0000000")


@pytest.mark.asyncio
async def test_read_big_file_offset_beyond_scan_budget_errors(
        tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    big = _big_text_file(tmp_path)
    monkeypatch.setattr(file_service_module, "_RANGE_SCAN_MAX_BYTES",
                        1024 * 1024)

    result = await _read_file("proj", "sess", str(big), offset=290_000)
    assert result.startswith("Error:")
    assert "scan budget" in result


@pytest.mark.asyncio
async def test_read_big_file_tail_beyond_scan_budget_errors(
        tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    big = _big_text_file(tmp_path)
    monkeypatch.setattr(file_service_module, "_RANGE_SCAN_MAX_BYTES",
                        1024 * 1024)

    result = await _read_file("proj", "sess", str(big), limit=-5)
    assert result.startswith("Error:")
    assert "last lines" in result


@pytest.mark.asyncio
async def test_read_directory_returns_friendly_error(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "dir").mkdir()
    result = await _read_file("proj", "sess", "dir")
    assert result.startswith("Error:")
    assert "Not a regular file" in result


# ── D: edit line-ending/encoding round-trips + region gate ───────────

@pytest.mark.asyncio
async def test_edit_crlf_file_matches_and_preserves_endings(
        tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "win.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    await _read_file("proj", "sess", "win.txt")
    result = await _edit_file("proj", "sess", "win.txt", "beta\n", "BETA\n")
    assert result.startswith("File edited:")
    # CRLF is restored on write-back, not silently converted to LF.
    assert target.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


@pytest.mark.asyncio
async def test_edit_utf16_file_round_trip(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "u16.txt"
    target.write_bytes("héllo wörld\n".encode("utf-16"))  # BOM + LE

    read_result = await _read_file("proj", "sess", "u16.txt")
    assert "héllo wörld" in read_result  # no false "binary" rejection

    result = await _edit_file("proj", "sess", "u16.txt", "héllo", "goodbye")
    assert result.startswith("File edited:")
    raw = target.read_bytes()
    assert raw.startswith(b"\xff\xfe")  # UTF-16 LE BOM preserved
    assert raw.decode("utf-16") == "goodbye wörld\n"


@pytest.mark.asyncio
async def test_edit_utf16be_file_round_trip_preserves_byte_order(
        tmp_path, monkeypatch):
    """A big-endian UTF-16 file must round-trip as big-endian; writing back
    with the native-order "utf-16" codec would flip the whole file."""
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "u16be.txt"
    target.write_bytes(b"\xfe\xff" + "héllo wörld\n".encode("utf-16-be"))

    await _read_file("proj", "sess", "u16be.txt")
    result = await _edit_file("proj", "sess", "u16be.txt", "héllo", "goodbye")
    assert result.startswith("File edited:")

    raw = target.read_bytes()
    # Byte-exact: one BE BOM, BE-encoded body — no native-order flip.
    assert raw == b"\xfe\xff" + "goodbye wörld\n".encode("utf-16-be")


@pytest.mark.asyncio
async def test_edit_utf16_crlf_file_round_trip_preserves_endings(
        tmp_path, monkeypatch):
    """The CRLF heuristic runs on decoded text, so BOM'd UTF-16 CRLF files
    keep their line endings through an edit (UTF-16's raw bytes never
    contain "\r\n" as a contiguous pair)."""
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "u16crlf.txt"
    target.write_bytes("alpha\r\nbeta\r\n".encode("utf-16"))  # BOM + LE

    await _read_file("proj", "sess", "u16crlf.txt")
    result = await _edit_file("proj", "sess", "u16crlf.txt", "beta\n", "BETA\n")
    assert result.startswith("File edited:")
    assert target.read_bytes() == (
        b"\xff\xfe" + "alpha\r\nBETA\r\n".encode("utf-16-le"))


@pytest.mark.asyncio
async def test_edit_utf8_bom_preserved(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfhello\n")

    await _read_file("proj", "sess", "bom.txt")
    result = await _edit_file("proj", "sess", "bom.txt", "hello", "hi")
    assert result.startswith("File edited:")
    assert target.read_bytes() == b"\xef\xbb\xbfhi\n"


@pytest.mark.asyncio
async def test_edit_region_gate_blocks_unread_region(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "long.txt"
    target.write_text("\n".join(f"line {i}" for i in range(500)))

    await _read_file("proj", "sess", "long.txt", offset=0, limit=10)
    result = await _edit_file(
        "proj", "sess", "long.txt", "line 400", "edited")
    assert result.startswith("Error:")
    assert "outside the lines read" in result
    assert "offset=" in result


@pytest.mark.asyncio
async def test_edit_region_gate_allows_window_edit(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "long.txt"
    target.write_text(
        "\n".join(f"line {i} unique-{i:04d}" for i in range(500)))

    await _read_file("proj", "sess", "long.txt", offset=5, limit=10)
    result = await _edit_file(
        "proj", "sess", "long.txt", "unique-0008", "edited")
    assert result.startswith("File edited:")


@pytest.mark.asyncio
async def test_edit_replace_all_requires_full_read(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "dup.txt"
    target.write_text(
        "\n".join("token" if i in (10, 400) else f"line {i}"
                  for i in range(500)))

    await _read_file("proj", "sess", "dup.txt", offset=0, limit=50)
    result = await _edit_file(
        "proj", "sess", "dup.txt", "token", "renamed", replace_all=True)
    assert result.startswith("Error:")
    assert "whole file" in result

    await _read_file("proj", "sess", "dup.txt", offset=0, limit=1000)
    result = await _edit_file(
        "proj", "sess", "dup.txt", "token", "renamed", replace_all=True)
    assert result.startswith("File edited:")


@pytest.mark.asyncio
async def test_edit_replace_all_full_read_with_trailing_newline(
        tmp_path, monkeypatch):
    """The window totals drop the phantom trailing line, so a complete read
    of a trailing-newline file still satisfies the replace_all gate."""
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "dup.txt"
    target.write_text("x token\ny token\n")

    await _read_file("proj", "sess", "dup.txt")
    result = await _edit_file(
        "proj", "sess", "dup.txt", "token", "renamed", replace_all=True)
    assert result.startswith("File edited:")
    assert target.read_text() == "x renamed\ny renamed\n"


@pytest.mark.asyncio
async def test_edit_region_gate_accumulates_windows(tmp_path, monkeypatch):
    """Coverage is the union of windows read: after several windowed reads,
    every read region is editable — not just the most recent window."""
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "long.txt"
    target.write_text("\n".join(f"line {i} unique-{i:04d}" for i in range(500)))

    await _read_file("proj", "sess", "long.txt", offset=0, limit=10)
    await _read_file("proj", "sess", "long.txt", offset=300, limit=10)

    # Never-read region still refused (checked first — a successful edit
    # refreshes coverage to the whole file).
    result = await _edit_file("proj", "sess", "long.txt", "line 150", "c")
    assert result.startswith("Error:")
    assert "outside the lines read" in result

    # Line 5 was read only by the *first* window — the union makes it
    # editable even though the latest read covers lines 301-310.
    result = await _edit_file("proj", "sess", "long.txt", "unique-0005", "a")
    assert result.startswith("File edited:")
    result = await _edit_file("proj", "sess", "long.txt", "unique-0305", "b")
    assert result.startswith("File edited:")


@pytest.mark.asyncio
async def test_edit_replace_all_allows_contiguous_full_coverage(
        tmp_path, monkeypatch):
    """Contiguous windowed reads that together span the whole file satisfy
    the replace_all whole-file requirement; a coverage gap does not."""
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "dup.txt"
    target.write_text("\n".join(
        "token" if i in (10, 400) else f"line {i}" for i in range(500)))

    # Gap: lines 251-300 never read.
    await _read_file("proj", "sess", "dup.txt", offset=0, limit=250)
    await _read_file("proj", "sess", "dup.txt", offset=300, limit=200)
    result = await _edit_file(
        "proj", "sess", "dup.txt", "token", "renamed", replace_all=True)
    assert result.startswith("Error:")
    assert "whole file" in result

    # Filling the gap completes the coverage; replace_all is now allowed.
    await _read_file("proj", "sess", "dup.txt", offset=250, limit=50)
    result = await _edit_file(
        "proj", "sess", "dup.txt", "token", "renamed", replace_all=True)
    assert result.startswith("File edited:")


@pytest.mark.asyncio
async def test_edit_region_gate_resets_on_disk_change(tmp_path, monkeypatch):
    """Coverage belongs to one file state: an external change discards old
    windows, so a fresh windowed read does not unlock earlier lines seen
    only before the change."""
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "long.txt"
    target.write_text("\n".join(f"line {i} unique-{i:04d}" for i in range(500)))
    await _read_file("proj", "sess", "long.txt", offset=0, limit=10)

    # External edit with a guaranteed-distinct mtime.
    lines = target.read_text().split("\n")
    lines[8] = "line 8 unique-0008-changed"
    target.write_text("\n".join(lines))
    st = target.stat()
    os.utime(target, (st.st_atime, st.st_mtime + 10))

    # Stale coverage blocks the edit until a re-read happens.
    result = await _edit_file("proj", "sess", "long.txt", "unique-0003", "x")
    assert result.startswith("Error:")
    assert "has not been read yet" in result

    # The re-read covers only lines 101-110; the pre-change window over
    # lines 1-10 must not survive the mtime change.
    await _read_file("proj", "sess", "long.txt", offset=100, limit=10)
    result = await _edit_file("proj", "sess", "long.txt", "unique-0003", "x")
    assert result.startswith("Error:")
    assert "outside the lines read" in result


@pytest.mark.asyncio
async def test_edit_and_preflight_refuse_notebooks(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "nb.ipynb"
    target.write_text('{"cells": [], "metadata": {}}')

    result = await _edit_file("proj", "sess", "nb.ipynb", "a", "b")
    assert result.startswith("Error:")
    assert "notebook_edit" in result

    pre = await _edit_preflight("proj", "sess", "nb.ipynb",
                                old_string="a", new_string="b")
    assert pre is not None and "notebook_edit" in pre


@pytest.mark.asyncio
async def test_edit_rejects_empty_old_string(tmp_path, monkeypatch):
    """Empty old_string (e.g. to fill an empty file) is rejected with a
    pointer to the write tool — an empty read window can never cover an
    edit, so the region gate would otherwise emit a dead-end error."""
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "empty.txt"
    target.write_text("")

    result = await _edit_file("proj", "sess", str(target), "", "hello")
    assert result.startswith("Error:")
    assert "non-empty" in result

    pre = await _edit_preflight("proj", "sess", str(target),
                                old_string="", new_string="hello")
    assert pre is not None and "non-empty" in pre


# ── F: image downscaling ─────────────────────────────────────────────

def _png_bytes_pil(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color=(120, 40, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_read_image_downscales_oversize_dimensions(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "wide.png"
    target.write_bytes(_png_bytes_pil(4000, 3000))

    result = await _read_image("proj", "sess", str(target), ".png")
    assert isinstance(result, dict), result
    assert result["media_type"] == "image/png"
    assert "downscaled from 4000×3000" in result["text"]
    img = Image.open(io.BytesIO(base64.b64decode(result["image_base64"])))
    assert max(img.size) <= 3840


@pytest.mark.asyncio
async def test_read_image_without_pil_rejects_oversize(tmp_path, monkeypatch):
    pytest.importorskip("PIL")  # build the image, then hide PIL
    _patch_file_service(monkeypatch, tmp_path)
    target = tmp_path / "wide.png"
    target.write_bytes(_png_bytes_pil(4000, 3000))

    monkeypatch.setattr(file_tools, "_PIL_AVAILABLE", False)
    result = await _read_image("proj", "sess", str(target), ".png")
    assert isinstance(result, str)
    assert "exceeds" in result


# ── C: cancellation reaches in-flight tools ──────────────────────────

class _RunnerHarness:
    """LLMLoopRunner without running its __init__ (heavy dependencies)."""

    @classmethod
    def make(cls):
        from app.services.llm_loop_runner import LLMLoopRunner
        return LLMLoopRunner.__new__(LLMLoopRunner)


@pytest.mark.asyncio
async def test_execute_tool_cancellable_cancels_slow_tool():
    runner = _RunnerHarness.make()
    ctx = SimpleNamespace(cancel_event=asyncio.Event())

    async def set_cancel_later():
        await asyncio.sleep(0.1)
        ctx.cancel_event.set()

    setter = asyncio.create_task(set_cancel_later())
    start = time.monotonic()
    result = await runner._execute_tool_cancellable(
        ctx, asyncio.sleep(30))
    elapsed = time.monotonic() - start
    assert result == "Tool cancelled by user."
    assert elapsed < 5.0
    assert setter.done()


@pytest.mark.asyncio
async def test_execute_tool_cancellable_propagates_exceptions():
    runner = _RunnerHarness.make()
    ctx = SimpleNamespace(cancel_event=asyncio.Event())

    async def boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await runner._execute_tool_cancellable(ctx, boom())


@pytest.mark.asyncio
async def test_execute_tool_cancellable_outer_cancel_cleans_up_tool():
    """Cancelling the runner task itself (worker shutdown) must not orphan
    the in-flight tool — its CancelledError handler is what kills the
    tool's subprocesses."""
    runner = _RunnerHarness.make()
    ctx = SimpleNamespace(cancel_event=asyncio.Event())
    tool_cancelled = asyncio.Event()
    tool_started = asyncio.Event()

    async def slow_tool():
        tool_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise

    task = asyncio.create_task(
        runner._execute_tool_cancellable(ctx, slow_tool()))
    await tool_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tool_cancelled.is_set()


@pytest.mark.asyncio
async def test_execute_tool_cancellable_without_event_awaits_normally():
    runner = _RunnerHarness.make()
    ctx = SimpleNamespace(cancel_event=None)
    result = await runner._execute_tool_cancellable(ctx, _return("ok"))
    assert result == "ok"


async def _return(value):
    await asyncio.sleep(0)
    return value


def _procs_containing(marker: str) -> list:
    """PIDs whose cmdline contains *marker* (avoids pgrep's self-match)."""
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace")
        except OSError:
            continue
        if marker in cmdline:
            hits.append((pid, cmdline))
    return hits


def _pgid_of(pid: str) -> int:
    """Process group id from /proc/<pid>/stat (field 5)."""
    with open(f"/proc/{pid}/stat") as f:
        # comm may contain spaces/parens — parse after the last ')'.
        return int(f.read().rsplit(")", 1)[1].split()[2])


def _pids_in_pgid(pgid: int) -> list:
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if _pgid_of(pid) == pgid:
                hits.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return hits


@pytest.mark.asyncio
async def test_bash_cancellation_kills_subprocess(tmp_path, monkeypatch):
    from app.agents.tools import bash as bash_module
    from app.agents.tools.bash import _run_bash
    monkeypatch.setattr(
        bash_module, "settings",
        SimpleNamespace(get_project_path=lambda pid: tmp_path))

    marker = "sigma_cancel_probe_9d41"
    task = asyncio.create_task(
        _run_bash("proj", f"sleep 300; echo {marker}", timeout=600))
    await asyncio.sleep(0.5)
    # The shell (and its sleep child) must be running before cancellation.
    shells = _procs_containing(marker)
    assert shells, "command did not start"
    pgid = _pgid_of(shells[0][0])
    assert len(_pids_in_pgid(pgid)) >= 2, "child process not in group"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The whole process group must be dead — killing only the coroutine (or
    # only the shell) would orphan the sleep child.
    await asyncio.sleep(0.3)
    assert not _pids_in_pgid(pgid), \
        "bash process group survived task cancellation"
