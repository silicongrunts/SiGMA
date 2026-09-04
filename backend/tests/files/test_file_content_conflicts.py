"""Frontend-save hash conflicts vs the tool write path.

Both go through ``file_service.write_file``; the frontend variant demands a
matching ``expected_hash`` for existing files while the agent tool variant
may still overwrite without one.
"""
import pytest

from app.services.file_service import file_service


@pytest.mark.asyncio
async def test_frontend_save_requires_hash_for_existing_file(sandbox_without_snapshots):
    path = sandbox_without_snapshots / "main.tex"
    path.write_text("disk version\n", encoding="utf-8")

    result = await file_service.write_file(
        "project",
        "main.tex",
        "editor version\n",
        require_expected_hash=True,
    )

    assert result["conflict"] is True
    assert path.read_text(encoding="utf-8") == "disk version\n"


@pytest.mark.asyncio
async def test_frontend_save_conflicts_on_stale_hash(sandbox_without_snapshots):
    path = sandbox_without_snapshots / "main.tex"
    base_hash = file_service.compute_hash("base\n")
    path.write_text("disk version\n", encoding="utf-8")

    result = await file_service.write_file(
        "project",
        "main.tex",
        "editor version\n",
        expected_hash=base_hash,
        require_expected_hash=True,
    )

    assert result["conflict"] is True
    assert path.read_text(encoding="utf-8") == "disk version\n"


@pytest.mark.asyncio
async def test_tool_write_can_still_update_existing_file_without_hash(
    sandbox_without_snapshots,
):
    path = sandbox_without_snapshots / "main.tex"
    path.write_text("disk version\n", encoding="utf-8")

    result = await file_service.write_file("project", "main.tex", "tool version\n")

    assert result["conflict"] is False
    assert result["hash"] == file_service.compute_hash("tool version\n")
    assert path.read_text(encoding="utf-8") == "tool version\n"
