"""
Tests for projects.json atomic read-modify-write and corruption handling.

Uses the shared ``ps`` fixture (tests/projects/conftest.py) pointing a
ProjectService at a temporary directory.
"""

import asyncio
import json

import pytest

from app.core.exceptions import FileSystemError


# ---------------------------------------------------------------------------
# Concurrent read-modify-write
# ---------------------------------------------------------------------------

@pytest.mark.concurrency
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_concurrent_update_projects(ps):
    """20 _update_projects calls from concurrent threads produce 20 keys
    (no lost writes). The registry write is a synchronous critical section,
    so real contention requires real threads — plain asyncio tasks would
    run it sequentially and never contend the ProjectFileLock."""
    async def write(i: int):
        (ps.USERDATA_DIR / f"p{i}").mkdir()
        await asyncio.to_thread(
            ps._update_projects,
            lambda p, i=i: p.update({f"p{i}": {"name": f"P{i}"}}),
        )

    await asyncio.gather(*[write(i) for i in range(20)])

    data = json.loads(ps.PROJECTS_FILE.read_text())
    assert len(data) == 20, f"Expected 20 projects, got {len(data)}"


# ---------------------------------------------------------------------------
# Concurrent create + delete
# ---------------------------------------------------------------------------

@pytest.mark.concurrency
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_concurrent_create_and_delete(ps):
    """Interleaved creates and deletes from concurrent threads don't
    corrupt projects.json or lose writes."""
    async def create(i: int):
        (ps.USERDATA_DIR / f"p{i}").mkdir()
        await asyncio.to_thread(
            ps._update_projects,
            lambda p, i=i: p.update({f"p{i}": {"name": f"P{i}"}}),
        )

    async def delete(key: str):
        await asyncio.to_thread(ps._update_projects, lambda p: p.pop(key, None))

    # Create 10
    await asyncio.gather(*[create(i) for i in range(10)])

    # Delete 5 while creating 5 more
    await asyncio.gather(
        *[delete(f"p{i}") for i in range(5)],
        *[create(10 + i) for i in range(5)],
    )

    data = json.loads(ps.PROJECTS_FILE.read_text())
    expected = {f"p{i}" for i in range(5, 15)}
    assert set(data.keys()) == expected


# ---------------------------------------------------------------------------
# Corrupt JSON — read path raises typed error
# ---------------------------------------------------------------------------

def test_corrupt_json_read_raises_typed_error(ps):
    """_load_projects_readonly raises FileSystemError on corrupt JSON."""
    ps.PROJECTS_FILE.write_text("{bad json!!", encoding="utf-8")
    with pytest.raises(FileSystemError, match="corrupt"):
        ps._load_projects_readonly()


def test_corrupt_json_is_backed_up(ps):
    """Corrupt file is backed up with timestamp before raising."""
    ps.PROJECTS_FILE.write_text("{bad json!!", encoding="utf-8")
    with pytest.raises(FileSystemError):
        ps._load_projects_readonly()
    backups = list(ps.SIGMA_DIR.glob("projects.json.corrupt.*"))
    assert len(backups) == 1, f"Expected 1 backup, found {len(backups)}"
    assert backups[0].read_text() == "{bad json!!"


# ---------------------------------------------------------------------------
# Corrupt JSON — write path fail-fast
# ---------------------------------------------------------------------------

def test_corrupt_json_write_path_fails_fast(ps):
    """_update_projects raises CORRUPT_PROJECT_INDEX on corrupt JSON."""
    ps.PROJECTS_FILE.write_text("{bad!!", encoding="utf-8")
    with pytest.raises(FileSystemError, match="corrupt"):
        ps._update_projects(lambda p: p.update({"new": {"name": "x"}}))
    # The corrupt file should have been backed up
    backups = list(ps.SIGMA_DIR.glob("projects.json.corrupt.*"))
    assert len(backups) == 1


def test_corrupt_json_write_does_not_overwrite(ps):
    """Write-path failure on corrupt JSON does not create a new empty file."""
    ps.PROJECTS_FILE.write_text('{"existing": {"name": "MyProject"}}', encoding="utf-8")
    # Deliberately corrupt it
    ps.PROJECTS_FILE.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(FileSystemError):
        ps._update_projects(lambda p: p.update({"new": {"name": "x"}}))
    # projects.json should not exist (backed up, not overwritten)
    assert not ps.PROJECTS_FILE.exists()


# ---------------------------------------------------------------------------
# Atomic write produces valid JSON
# ---------------------------------------------------------------------------

def test_save_produces_valid_json(ps):
    """_update_projects always produces parseable JSON."""
    for i in range(50):
        ps._update_projects(lambda p, i=i: p.update({f"k{i}": i}))
    data = json.loads(ps.PROJECTS_FILE.read_text())
    assert len(data) == 50
