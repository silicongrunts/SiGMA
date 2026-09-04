"""Tests for snapshot triggering on absolute-path writes.

Absolute-path writes that land inside the project sandbox must trigger
auto-snapshot (mirroring relative-path writes). Writes outside the sandbox
must not. The project-lifecycle write gate is patched open by the shared
conftest fixture — the snapshot decision, not lifecycle gating, is what
these tests exercise.
"""

import pytest

from app.services.file_service import file_service


@pytest.mark.asyncio
async def test_write_absolute_inside_sandbox_triggers_snapshot(tmp_path, monkeypatch):
    """A write to an absolute path resolving inside the sandbox should
    call _after_file_mutation for the owning project."""
    triggered = []

    async def fake_notify(project_id):
        triggered.append(project_id)

    monkeypatch.setattr(file_service, "_after_file_mutation", fake_notify)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: sandbox)

    # classify_path will resolve to SANDBOX for any path under the sandbox
    target = sandbox / "subdir" / "written.txt"
    await file_service.write_file_absolute("proj", str(target), "content")

    assert triggered == ["proj"]


@pytest.mark.asyncio
async def test_write_absolute_outside_sandbox_skips_snapshot(tmp_path, monkeypatch):
    """A write outside the sandbox (e.g. to /tmp) must not trigger snapshot."""
    triggered = []

    async def fake_notify(project_id):
        triggered.append(project_id)

    monkeypatch.setattr(file_service, "_after_file_mutation", fake_notify)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: sandbox)

    # A sibling of the sandbox — outside the project root yet fully inside
    # tmp_path, so the test writes nothing outside pytest-owned directories.
    outside = tmp_path / "outside_sigmma_test_file.txt"
    await file_service.write_file_absolute("proj", str(outside), "content")

    assert triggered == []  # no snapshot triggered
    assert outside.read_text() == "content"


# Note: classify_path's branches are exercised by other tests in the suite
# (permission_executor tests); the two tests above pin the snapshot trigger
# condition (SANDBOX vs. non-SANDBOX writes) behaviorally, so no separate
# classifier sanity check is needed here.
