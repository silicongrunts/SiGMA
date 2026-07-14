"""Tests for snapshot triggering on absolute-path writes.

Absolute-path writes that land inside the project sandbox must trigger
auto-snapshot (mirroring relative-path writes). Writes outside the sandbox
must not.
"""

import pytest

from app.services.file_service import file_service, PathAccessLevel


@pytest.mark.asyncio
async def test_write_absolute_inside_sandbox_triggers_snapshot(tmp_path, monkeypatch):
    """A write to an absolute path resolving inside the sandbox should
    call _notify_snapshot for the owning project."""
    triggered = []

    async def fake_notify(project_id):
        triggered.append(project_id)

    monkeypatch.setattr(file_service, "_notify_snapshot", fake_notify)
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)

    # classify_path will resolve to SANDBOX for any path under tmp_path
    target = tmp_path / "subdir" / "written.txt"
    await file_service.write_file_absolute("proj", str(target), "content")

    assert triggered == ["proj"]


@pytest.mark.asyncio
async def test_write_absolute_outside_sandbox_skips_snapshot(tmp_path, monkeypatch):
    """A write outside the sandbox (e.g. to /tmp) must not trigger snapshot."""
    triggered = []

    async def fake_notify(project_id):
        triggered.append(project_id)

    monkeypatch.setattr(file_service, "_notify_snapshot", fake_notify)
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)

    # Path outside the project sandbox
    outside = tmp_path.parent / "outside_sigmma_test_file.txt"
    try:
        await file_service.write_file_absolute("proj", str(outside), "content")
        assert triggered == []  # no snapshot triggered
    finally:
        if outside.exists():
            outside.unlink()


@pytest.mark.asyncio
async def test_classify_path_returns_sandbox_for_inner_path(tmp_path, monkeypatch):
    """Sanity check for the path classifier used in snapshot decision."""
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)
    level = file_service.classify_path("proj", str(tmp_path / "deep" / "file.txt"))
    assert level is PathAccessLevel.SANDBOX


# Note: classify_path's EXTERNAL/TMP branches are exercised by other tests
# in the suite (permission_executor tests); we only need to verify SANDBOX
# detection here since that's the trigger condition.
