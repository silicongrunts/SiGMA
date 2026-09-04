"""End-to-end tests for the must-read-first contract.

Covers the integration between the ``read_state_cache`` and the ``write`` /
``edit`` tools: a write or edit must succeed only if the target file has been
read in the same session, and must fail after a compaction (cache clear) or if
the file has been modified on disk since the read.
"""

import os

import pytest

from app.agents.tools.file_tools import _read_file, _write_file, _edit_file
from app.agents.tools.read_state import read_state_cache


# ── write ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_new_file_does_not_require_prior_read(sandbox):
    result = await _write_file("proj", "sess", "new.txt", "hello")
    assert result.startswith("File written:")
    assert (sandbox / "new.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_write_existing_file_requires_prior_read(sandbox):
    (sandbox / "exists.txt").write_text("old")

    result = await _write_file("proj", "sess", "exists.txt", "new")
    assert result.startswith("Error:")
    assert "has not been read" in result


@pytest.mark.asyncio
async def test_write_succeeds_after_read(sandbox):
    (sandbox / "exists.txt").write_text("old")

    await _read_file("proj", "sess", "exists.txt")
    result = await _write_file("proj", "sess", "exists.txt", "new")
    assert result.startswith("File written:")
    assert (sandbox / "exists.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_write_succeeds_after_partial_read(sandbox):
    (sandbox / "multi.txt").write_text("\n".join(str(i) for i in range(50)))

    # Paginated reads satisfy the must-read-first contract.
    await _read_file("proj", "sess", "multi.txt", offset=0, limit=5)
    result = await _write_file("proj", "sess", "multi.txt", "overwritten")
    assert result.startswith("File written:")
    assert (sandbox / "multi.txt").read_text() == "overwritten"


@pytest.mark.asyncio
async def test_write_succeeds_with_equivalent_absolute_path(sandbox):
    target = sandbox / "same.txt"
    target.write_text("old")

    await _read_file("proj", "sess", "same.txt")
    result = await _write_file("proj", "sess", str(target), "new")
    assert result.startswith("File written:")
    assert target.read_text() == "new"


@pytest.mark.asyncio
async def test_write_fails_after_compaction_clears_cache(sandbox):
    (sandbox / "f.txt").write_text("v1")

    await _read_file("proj", "sess", "f.txt")
    # Simulate compaction
    read_state_cache.clear("sess")
    result = await _write_file("proj", "sess", "f.txt", "v2")
    assert result.startswith("Error:")
    assert "has not been read" in result


@pytest.mark.asyncio
async def test_write_fails_when_file_modified_since_read(sandbox):
    target = sandbox / "f.txt"
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
async def test_edit_requires_prior_read(sandbox):
    (sandbox / "e.txt").write_text("hello world")

    result = await _edit_file(
        "proj", "sess", "e.txt", "hello", "goodbye",
    )
    assert result.startswith("Error:")
    assert "has not been read" in result


@pytest.mark.asyncio
async def test_edit_succeeds_after_read(sandbox):
    (sandbox / "e.txt").write_text("hello world")

    await _read_file("proj", "sess", "e.txt")
    result = await _edit_file("proj", "sess", "e.txt", "hello", "goodbye")
    assert result.startswith("File edited:")
    assert (sandbox / "e.txt").read_text() == "goodbye world"


@pytest.mark.asyncio
async def test_edit_succeeds_after_partial_read(sandbox):
    (sandbox / "e.txt").write_text("hello world")

    await _read_file("proj", "sess", "e.txt", offset=0, limit=1)
    result = await _edit_file("proj", "sess", "e.txt", "hello", "goodbye")
    assert result.startswith("File edited:")
    assert (sandbox / "e.txt").read_text() == "goodbye world"


@pytest.mark.asyncio
async def test_edit_succeeds_with_equivalent_dot_relative_path(sandbox):
    target = sandbox / "e.txt"
    target.write_text("hello world")

    await _read_file("proj", "sess", str(target))
    result = await _edit_file("proj", "sess", "./e.txt", "hello", "goodbye")
    assert result.startswith("File edited:")
    assert target.read_text() == "goodbye world"


@pytest.mark.asyncio
async def test_edit_identical_strings_rejected(sandbox):
    (sandbox / "e.txt").write_text("hello")

    await _read_file("proj", "sess", "e.txt")
    result = await _edit_file("proj", "sess", "e.txt", "hello", "hello")
    assert result.startswith("Error:")
    assert "identical" in result


@pytest.mark.asyncio
async def test_edit_non_unique_old_string_rejected(sandbox):
    (sandbox / "e.txt").write_text("dup dup")

    await _read_file("proj", "sess", "e.txt")
    result = await _edit_file("proj", "sess", "e.txt", "dup", "one", replace_all=False)
    assert "appears 2 times" in result


@pytest.mark.asyncio
async def test_edit_replace_all(sandbox):
    (sandbox / "e.txt").write_text("dup dup")

    await _read_file("proj", "sess", "e.txt")
    result = await _edit_file("proj", "sess", "e.txt", "dup", "x", replace_all=True)
    assert result.startswith("File edited:")
    assert (sandbox / "e.txt").read_text() == "x x"


# ── cross-tool: read then edit then write ────────────────────────────

@pytest.mark.asyncio
async def test_edit_refreshes_cache_allowing_subsequent_write(sandbox):
    """After a successful edit, the cache is refreshed — a same-turn write
    does not require another read."""
    (sandbox / "e.txt").write_text("hello world")

    await _read_file("proj", "sess", "e.txt")
    await _edit_file("proj", "sess", "e.txt", "hello", "goodbye")
    result = await _write_file("proj", "sess", "e.txt", "fresh content")
    assert result.startswith("File written:")


# ── session-less sub-loops: annotation / agent scope keys ────────────
#
# AnnotationLoop and agent sub-loops (explore, fork) have no session row, so
# they pass a stable namespace key ("annotation:<id>" / "agent:<kind>:<uuid>")
# instead of a real session_id. The must-read-first contract must still work
# within one scope and must NOT leak across scopes — otherwise a file read in
# annotation A would let annotation B edit it without reading.


@pytest.mark.asyncio
async def test_write_succeeds_after_read_with_annotation_scope_key(sandbox):
    (sandbox / "f.txt").write_text("old")

    await _read_file("proj", "annotation:ann-1", "f.txt")
    result = await _write_file("proj", "annotation:ann-1", "f.txt", "new")
    assert result.startswith("File written:")
    assert (sandbox / "f.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_read_state_is_isolated_between_annotation_scopes(sandbox):
    (sandbox / "shared.txt").write_text("base")

    # Annotation A reads the file.
    await _read_file("proj", "annotation:ann-A", "shared.txt")

    # Annotation B must NOT inherit A's read-state: the must-read-first
    # contract should block the write.
    result = await _write_file("proj", "annotation:ann-B", "shared.txt", "B-edit")
    assert result.startswith("Error:")
    assert "has not been read" in result

    # A can still edit (its own read-state is intact).
    result = await _write_file("proj", "annotation:ann-A", "shared.txt", "A-edit")
    assert result.startswith("File written:")


@pytest.mark.asyncio
async def test_read_state_is_isolated_between_agent_scopes(sandbox):
    (sandbox / "shared.txt").write_text("base")

    # One agent fork reads the file.
    await _read_file("proj", "agent:fork:aaa", "shared.txt")

    # A different fork must not inherit the prior read.
    result = await _write_file("proj", "agent:fork:bbb", "shared.txt", "edit")
    assert result.startswith("Error:")
    assert "has not been read" in result
