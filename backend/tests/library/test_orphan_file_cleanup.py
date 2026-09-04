"""Orphan-file cleanup must never unlink in-flight uploads.

A concurrent upload's temp lives in the library's ``.uploading`` subdirectory
and a just-landed file is unreferenced until its DB row commits, so cleanup
only removes unreferenced files (and stale upload temps) older than the
grace period. The daily upkeep pass reuses the same sweep.

All tests run against a real per-project database (the shared ``project``
fixture), so the sigma path and settings are the production ones.
"""

import os
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.database.unit_of_work import UnitOfWork
from app.services import background_task_service as bts
from app.services.library_service import library_service

ORPHAN_AGE_SECONDS = 2 * 3600


@pytest.fixture
def library_dir(project):
    """The project's real library directory, created on demand."""
    path = settings.get_sigma_path(project) / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _age_file(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


async def test_cleanup_keeps_recent_unreferenced_file(project, library_dir):
    fresh = library_dir / "landing.pdf"
    fresh.write_bytes(b"partial landing, row not committed yet")

    await library_service._cleanup_orphan_files(project, set())

    assert fresh.exists()


async def test_cleanup_removes_old_unreferenced_file(project, library_dir):
    orphan = library_dir / "orphan.pdf"
    orphan.write_bytes(b"orphan")
    _age_file(orphan, ORPHAN_AGE_SECONDS)

    await library_service._cleanup_orphan_files(project, set())

    assert not orphan.exists()


async def test_cleanup_keeps_old_referenced_file(project, library_dir):
    referenced = library_dir / "kept.pdf"
    referenced.write_bytes(b"referenced")
    _age_file(referenced, ORPHAN_AGE_SECONDS)

    await library_service._cleanup_orphan_files(project, {str(referenced)})

    assert referenced.exists()


async def test_cleanup_keeps_in_flight_upload_temp(project, library_dir):
    uploading = library_dir / ".uploading"
    uploading.mkdir()
    temp = uploading / ".upload_abc.pdf"
    temp.write_bytes(b"stream in progress")

    await library_service._cleanup_orphan_files(project, set())

    assert temp.exists()


async def test_cleanup_removes_stale_crashed_upload_temp(project, library_dir):
    uploading = library_dir / ".uploading"
    uploading.mkdir()
    temp = uploading / ".upload_abc.pdf"
    temp.write_bytes(b"left by a crashed upload")
    _age_file(temp, ORPHAN_AGE_SECONDS)

    await library_service._cleanup_orphan_files(project, set())

    assert not temp.exists()


# ---------------------------------------------------------------------------
# Daily upkeep pass
# ---------------------------------------------------------------------------

async def _reference_file(project_id, path: Path):
    async with UnitOfWork(project_id) as uow:
        await uow.library.create(title=path.name, content="", file_path=str(path))


async def test_daily_upkeep_removes_stale_orphan_file(project, library_dir):
    orphan = library_dir / "orphan.pdf"
    orphan.write_bytes(b"abandoned by a crash")
    _age_file(orphan, ORPHAN_AGE_SECONDS)

    await bts.background_task_service._cleanup_project_orphan_files(project)

    assert not orphan.exists()


async def test_daily_upkeep_keeps_recent_and_referenced_files(project, library_dir):
    fresh = library_dir / "fresh.pdf"
    fresh.write_bytes(b"row not committed yet")
    referenced = library_dir / "referenced.pdf"
    referenced.write_bytes(b"owned by a document")
    _age_file(referenced, ORPHAN_AGE_SECONDS)
    await _reference_file(project, referenced)

    await bts.background_task_service._cleanup_project_orphan_files(project)

    assert fresh.exists()
    assert referenced.exists()
