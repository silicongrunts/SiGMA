"""Tests for the glob and grep tools — sorting, prefixing, and output modes."""

import os
import time

import pytest

from app.agents.tools import file_tools
from app.agents.tools.file_tools import _glob_search, _grep_fallback, _grep_search


def _set_mtime(path, mtime):
    st = path.stat()
    os.utime(path, (st.st_atime, mtime))


@pytest.fixture(autouse=True)
def _relaxed_search_deadlines(monkeypatch):
    """Widen the bounded-search wall-clock deadlines for this file.

    The rg/grep budgets in file_tools (15s grep / 20s glob) are production
    constants with no settings or parameter injection. They bound a full
    directory walk, but the deadline clock also covers process spawn and
    event-loop scheduling; on a loaded machine (e.g. a real SiGMA instance
    running alongside the suite) a one-file sandbox search has been observed
    to burn the whole budget, turning a deterministic search into a
    spurious "search timed out" (no-match tests) or truncated output. These
    searches cover a handful of tiny files, so a generous deadline only
    delays pathological cases; no assertion depends on the timeout value.
    """
    monkeypatch.setattr(file_tools, "_GREP_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(file_tools, "_GLOB_TIMEOUT_SECONDS", 60.0)


# ── glob: sorting ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_glob_sorts_by_mtime_desc(sandbox):
    old = sandbox / "old.txt"
    new = sandbox / "new.txt"
    old.write_text("a")
    new.write_text("b")
    _set_mtime(old, time.time() - 1000)
    _set_mtime(new, time.time())

    result = await _glob_search("proj", "*.txt", ".")
    lines = result.split("\n")
    assert lines[0] == "new.txt"
    assert lines[1] == "old.txt"


@pytest.mark.asyncio
async def test_glob_alphabetical_tiebreak(sandbox):
    same_time = time.time()
    for name in ["c.txt", "a.txt", "b.txt"]:
        p = sandbox / name
        p.write_text("x")
        _set_mtime(p, same_time)

    result = await _glob_search("proj", "*.txt", ".")
    assert result.split("\n") == ["a.txt", "b.txt", "c.txt"]


# ── glob: subdirectory path prefix ──────────────────────────────────

@pytest.mark.asyncio
async def test_glob_relative_subdir_prepends_prefix(sandbox):
    sub = sandbox / "src"
    sub.mkdir()
    (sub / "foo.ts").write_text("")
    (sub / "bar.ts").write_text("")

    result = await _glob_search("proj", "**/*.ts", "src")
    lines = result.split("\n")
    assert "src/foo.ts" in lines
    assert "src/bar.ts" in lines
    # No bare names should leak
    assert "foo.ts" not in lines


@pytest.mark.asyncio
async def test_glob_root_path_returns_unprefixed(sandbox):
    (sandbox / "top.txt").write_text("")
    sub = sandbox / "src"
    sub.mkdir()
    (sub / "nested.txt").write_text("")

    result = await _glob_search("proj", "**/*.txt", ".")
    lines = result.split("\n")
    assert "top.txt" in lines
    assert "src/nested.txt" in lines


@pytest.mark.asyncio
async def test_glob_absolute_path_returns_absolute_sorted_by_mtime(sandbox):
    sub = sandbox / "src"
    sub.mkdir()
    older = sub / "aa.py"  # alphabetically first, but older
    newer = sub / "zz.py"
    older.write_text("a")
    newer.write_text("b")
    _set_mtime(older, time.time() - 1000)
    _set_mtime(newer, time.time())

    result = await _glob_search("proj", "*.py", str(sub))
    assert result.split("\n") == [str(newer), str(older)]


# ── glob: truncation marker ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_glob_truncation_marker(sandbox):
    # Create more than 100 files
    for i in range(120):
        (sandbox / f"f{i:03d}.txt").write_text("")

    result = await _glob_search("proj", "*.txt", ".")
    assert "... (20 more matches not shown)" in result


@pytest.mark.asyncio
async def test_glob_empty_returns_no_files_message(sandbox):
    result = await _glob_search("proj", "*.nonexistent", ".")
    assert "No files matching" in result


# ── grep: output_mode ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_content_mode_returns_matching_lines(sandbox):
    (sandbox / "a.py").write_text("def foo():\n    return 'bar'\n")
    (sandbox / "b.py").write_text("import os\n")

    result = await _grep_search(
        "proj", "foo", ".", output_mode="content",
        flags={"-n": True}, head_limit=10, offset=0,
    )
    assert "foo" in result
    assert "a.py" in result


@pytest.mark.asyncio
async def test_grep_files_with_matches_mode_returns_paths_only(sandbox):
    (sandbox / "a.py").write_text("foo = 1\n")
    (sandbox / "b.py").write_text("bar = 1\n")

    result = await _grep_search(
        "proj", "foo", ".", output_mode="files_with_matches",
        flags={}, head_limit=10, offset=0,
    )
    assert "a.py" in result
    assert "b.py" not in result


@pytest.mark.asyncio
async def test_grep_no_matches_message(sandbox):
    """A pattern with zero hits returns the explicit "No matches" message.

    Environment sensitivity: the bounded rg run is wall-clock limited, so a
    pathologically loaded machine can still surface a "search timed out"
    error here; the module's autouse fixture widens the deadline to keep
    this deterministic. Input is already minimal (one one-line file).
    """
    (sandbox / "a.py").write_text("hello\n")

    result = await _grep_search(
        "proj", "nomatch_xyz_zzz", ".",
        output_mode="content", flags={}, head_limit=10, offset=0,
    )
    assert "No matches" in result


# ── grep: pattern starting with hyphen (must use -e) ────────────────

@pytest.mark.asyncio
async def test_grep_pattern_starting_with_hyphen(sandbox):
    """A pattern starting with '-' must be passed via -e, not as a flag."""
    (sandbox / "a.txt").write_text("has -i flag-looking text\n")

    result = await _grep_search(
        "proj", "-i", ".",
        output_mode="content", flags={}, head_limit=10, offset=0,
    )
    # The literal "-i" string should be found, not interpreted as case-insensitive
    assert "a.txt" in result


# ── grep: case insensitive flag ─────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_case_insensitive_flag(sandbox):
    (sandbox / "a.txt").write_text("Hello World\n")

    # Without -i, "hello" shouldn't match
    result_sensitive = await _grep_search(
        "proj", "hello", ".",
        output_mode="content", flags={"-i": False}, head_limit=10, offset=0,
    )
    assert "No matches" in result_sensitive

    # With -i, it should match
    result_insensitive = await _grep_search(
        "proj", "hello", ".",
        output_mode="content", flags={"-i": True}, head_limit=10, offset=0,
    )
    assert "a.txt" in result_insensitive


# ── grep: glob filter ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_glob_filter(sandbox):
    (sandbox / "match.py").write_text("target_token\n")
    (sandbox / "match.txt").write_text("target_token\n")

    result = await _grep_search(
        "proj", "target_token", ".",
        glob_filter="*.py", output_mode="files_with_matches",
        flags={}, head_limit=10, offset=0,
    )
    assert "match.py" in result
    assert "match.txt" not in result


# ── grep: truncation marker ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_truncation_marker(sandbox):
    # Generate one file with many matching lines
    (sandbox / "big.txt").write_text("\n".join("match" for _ in range(300)))

    result = await _grep_search(
        "proj", "match", ".",
        output_mode="content", flags={"-n": True}, head_limit=10, offset=0,
    )
    assert "Showing results" in result
    assert "1-10 of 300" in result
    assert "290 more not shown" in result


@pytest.mark.asyncio
async def test_grep_head_limit_zero_means_unlimited(sandbox):
    (sandbox / "big.txt").write_text("\n".join("match" for _ in range(50)))

    result = await _grep_search(
        "proj", "match", ".",
        output_mode="content", flags={"-n": True}, head_limit=0, offset=0,
    )
    # 50 matching lines + 1 file:line prefix — no truncation marker
    assert "Showing results" not in result
    # All 50 matches returned
    assert result.count("\n") >= 49


# ── grep: project-relative output paths ─────────────────────────────

@pytest.mark.asyncio
async def test_grep_root_search_returns_project_relative_paths(sandbox):
    (sandbox / "src").mkdir()
    (sandbox / "src" / "a.py").write_text("target_token\n")
    (sandbox / "b.py").write_text("target_token\n")

    result = await _grep_search(
        "proj", "target_token", ".", output_mode="files_with_matches",
        flags={}, head_limit=10, offset=0,
    )
    assert set(result.split("\n")) == {"src/a.py", "b.py"}
    assert str(sandbox) not in result
    assert "./" not in result


@pytest.mark.asyncio
async def test_grep_subdir_search_returns_project_relative_paths(sandbox):
    (sandbox / "src").mkdir()
    (sandbox / "src" / "a.py").write_text("target_token\n")
    (sandbox / "top.py").write_text("target_token\n")

    result = await _grep_search(
        "proj", "target_token", "src", output_mode="content",
        flags={"-n": True}, head_limit=10, offset=0,
    )
    assert "src/a.py:1:target_token" in result
    assert "top.py" not in result


@pytest.mark.asyncio
async def test_grep_absolute_path_returns_absolute_paths(sandbox):
    (sandbox / "a.py").write_text("target_token\n")

    result = await _grep_search(
        "proj", "target_token", str(sandbox), output_mode="files_with_matches",
        flags={}, head_limit=10, offset=0,
    )
    assert result.strip() == str(sandbox / "a.py")


@pytest.mark.asyncio
async def test_grep_fallback_strips_dot_slash_prefix(sandbox):
    """The grep fallback passes "." for the root search and must strip the
    "./" prefixes grep echoes (rg is not involved in this path)."""
    (sandbox / "a.py").write_text("target_token\n")

    result = await _grep_fallback(
        "target_token", ".", str(sandbox), "",
        output_mode="content", case_insensitive=False,
        context=0, after_context=0, before_context=0,
        multiline=False, type_filter="",
        head_limit=10, offset=0,
    )
    assert "a.py:1:target_token" in result
    assert "./a.py" not in result
