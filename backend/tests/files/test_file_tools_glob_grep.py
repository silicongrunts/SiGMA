"""Tests for the glob and grep tools — sorting, prefixing, and output modes."""

import os
import time

import pytest

from app.agents.tools.file_tools import _glob_search, _grep_search


def _patch_file_service(monkeypatch, tmp_path):
    from app.services.file_service import file_service
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)


def _set_mtime(path, mtime):
    st = path.stat()
    os.utime(path, (st.st_atime, mtime))


# ── glob: sorting ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_glob_sorts_by_mtime_desc(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    old = tmp_path / "old.txt"
    new = tmp_path / "new.txt"
    old.write_text("a")
    new.write_text("b")
    _set_mtime(old, time.time() - 1000)
    _set_mtime(new, time.time())

    result = await _glob_search("proj", "*.txt", ".")
    lines = result.split("\n")
    assert lines[0] == "new.txt"
    assert lines[1] == "old.txt"


@pytest.mark.asyncio
async def test_glob_alphabetical_tiebreak(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    same_time = time.time()
    for name in ["c.txt", "a.txt", "b.txt"]:
        p = tmp_path / name
        p.write_text("x")
        _set_mtime(p, same_time)

    result = await _glob_search("proj", "*.txt", ".")
    assert result.split("\n") == ["a.txt", "b.txt", "c.txt"]


# ── glob: subdirectory path prefix ──────────────────────────────────

@pytest.mark.asyncio
async def test_glob_relative_subdir_prepends_prefix(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    sub = tmp_path / "src"
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
async def test_glob_root_path_returns_unprefixed(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "top.txt").write_text("")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "nested.txt").write_text("")

    result = await _glob_search("proj", "**/*.txt", ".")
    lines = result.split("\n")
    assert "top.txt" in lines
    assert "src/nested.txt" in lines


# ── glob: truncation marker ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_glob_truncation_marker(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    # Create more than 100 files
    for i in range(120):
        (tmp_path / f"f{i:03d}.txt").write_text("")

    result = await _glob_search("proj", "*.txt", ".")
    assert "more matches not shown" in result
    assert "20" in result  # 120 - 100


@pytest.mark.asyncio
async def test_glob_empty_returns_no_files_message(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    result = await _glob_search("proj", "*.nonexistent", ".")
    assert "No files matching" in result


# ── grep: output_mode ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_content_mode_returns_matching_lines(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("def foo():\n    return 'bar'\n")
    (tmp_path / "b.py").write_text("import os\n")

    result = await _grep_search(
        "proj", "foo", ".", output_mode="content",
        flags={"-n": True}, head_limit=10, offset=0,
    )
    assert "foo" in result
    assert "a.py" in result


@pytest.mark.asyncio
async def test_grep_files_with_matches_mode_returns_paths_only(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("foo = 1\n")
    (tmp_path / "b.py").write_text("bar = 1\n")

    result = await _grep_search(
        "proj", "foo", ".", output_mode="files_with_matches",
        flags={}, head_limit=10, offset=0,
    )
    assert "a.py" in result
    assert "b.py" not in result


@pytest.mark.asyncio
async def test_grep_no_matches_message(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("hello\n")

    result = await _grep_search(
        "proj", "nomatch_xyz_zzz", ".",
        output_mode="content", flags={}, head_limit=10, offset=0,
    )
    assert "No matches" in result


# ── grep: pattern starting with hyphen (must use -e) ────────────────

@pytest.mark.asyncio
async def test_grep_pattern_starting_with_hyphen(tmp_path, monkeypatch):
    """A pattern starting with '-' must be passed via -e, not as a flag."""
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("has -i flag-looking text\n")

    result = await _grep_search(
        "proj", "-i", ".",
        output_mode="content", flags={}, head_limit=10, offset=0,
    )
    # The literal "-i" string should be found, not interpreted as case-insensitive
    assert "a.txt" in result


# ── grep: case insensitive flag ─────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_case_insensitive_flag(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("Hello World\n")

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
async def test_grep_glob_filter(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "match.py").write_text("target_token\n")
    (tmp_path / "match.txt").write_text("target_token\n")

    result = await _grep_search(
        "proj", "target_token", ".",
        glob_filter="*.py", output_mode="files_with_matches",
        flags={}, head_limit=10, offset=0,
    )
    assert "match.py" in result
    assert "match.txt" not in result


# ── grep: truncation marker ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_truncation_marker(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    # Generate one file with many matching lines
    (tmp_path / "big.txt").write_text("\n".join("match" for _ in range(300)))

    result = await _grep_search(
        "proj", "match", ".",
        output_mode="content", flags={"-n": True}, head_limit=10, offset=0,
    )
    assert "Showing results" in result
    assert "1-10 of 300" in result
    assert "290 more not shown" in result


@pytest.mark.asyncio
async def test_grep_head_limit_zero_means_unlimited(tmp_path, monkeypatch):
    _patch_file_service(monkeypatch, tmp_path)
    (tmp_path / "big.txt").write_text("\n".join("match" for _ in range(50)))

    result = await _grep_search(
        "proj", "match", ".",
        output_mode="content", flags={"-n": True}, head_limit=0, offset=0,
    )
    # 50 matching lines + 1 file:line prefix — no truncation marker
    assert "Showing results" not in result
    # All 50 matches returned
    assert result.count("\n") >= 49
