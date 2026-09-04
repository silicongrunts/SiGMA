"""Streaming upload persistence through the file service.

Pins that ``save_upload`` streams the body straight to disk (bounded
chunks, atomic placement, no temp residue), rejects oversized bodies with
413, and settles the overwrite conflict on the final atomic claim of the
destination name — concurrent uploads of one name never overwrite each
other.
"""

import asyncio

import pytest

from app.core.config import settings
from app.core.exceptions import FileAlreadyExistsError, FileSystemError
from app.services.file_service import file_service
from tests.factories.uploads import ChunkedUpload


@pytest.fixture
def project_root(sandbox_without_snapshots, monkeypatch):
    """The shared snapshot-free sandbox, capped at a 1 MB upload limit."""
    monkeypatch.setattr(settings.files, "upload_max_mb", 1)
    return sandbox_without_snapshots


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_streams_body_to_destination(project_root):
    upload = ChunkedUpload(b"file body", filename="notes.md")

    saved = await file_service.save_upload("proj-1", "notes.md", upload, "docs")

    assert saved == "notes.md"
    dest = project_root / "docs" / "notes.md"
    assert dest.read_bytes() == b"file body"
    assert [p.name for p in dest.parent.iterdir()] == ["notes.md"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_accepts_body_at_cap(project_root):
    payload = b"y" * (settings.FILE_UPLOAD_MAX_MB * 1024 * 1024)
    upload = ChunkedUpload(payload, filename="big.bin")

    await file_service.save_upload("proj-1", "big.bin", upload, "")

    assert (project_root / "big.bin").read_bytes() == payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_rejects_oversize_with_413_and_no_residue(project_root):
    limit = settings.FILE_UPLOAD_MAX_MB * 1024 * 1024
    upload = ChunkedUpload(b"x" * (limit + 1), filename="big.bin")

    with pytest.raises(FileSystemError) as exc_info:
        await file_service.save_upload("proj-1", "big.bin", upload, "")

    assert exc_info.value.status_code == 413
    assert exc_info.value.code == "FILE_TOO_LARGE"
    assert list(project_root.iterdir()) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_conflict_keeps_existing_file(project_root):
    existing = project_root / "notes.md"
    existing.write_bytes(b"original")

    with pytest.raises(FileAlreadyExistsError):
        await file_service.save_upload(
            "proj-1", "notes.md", ChunkedUpload(b"new"), "",
            overwrite=False,
        )

    assert existing.read_bytes() == b"original"
    assert list(project_root.iterdir()) == [existing]


@pytest.mark.integration
@pytest.mark.concurrency
@pytest.mark.asyncio
async def test_concurrent_same_name_uploads_exactly_one_wins(project_root):
    """Two concurrent overwrite=False uploads of one name race only on the
    final atomic claim: exactly one succeeds, the loser gets
    FileAlreadyExistsError, and the winner's content lands intact — never a
    mix and no temp residue."""
    results = await asyncio.gather(
        file_service.save_upload(
            "proj-1", "notes.md",
            ChunkedUpload(b"first", filename="notes.md"), "",
            overwrite=False,
        ),
        file_service.save_upload(
            "proj-1", "notes.md",
            ChunkedUpload(b"second", filename="notes.md"), "",
            overwrite=False,
        ),
        return_exceptions=True,
    )

    assert sorted(isinstance(r, FileAlreadyExistsError) for r in results) == [False, True]
    dest = project_root / "notes.md"
    assert dest.read_bytes() in (b"first", b"second")
    assert list(project_root.iterdir()) == [dest]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_save_upload_overwrite_replaces_existing_file(project_root):
    existing = project_root / "notes.md"
    existing.write_bytes(b"original")

    await file_service.save_upload(
        "proj-1", "notes.md", ChunkedUpload(b"replaced"), "",
        overwrite=True,
    )

    assert existing.read_bytes() == b"replaced"
