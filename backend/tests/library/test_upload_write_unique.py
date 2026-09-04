"""Exclusive-claim upload landing.

Pins the ``_write_upload_unique`` contract: the final name is claimed with an
exclusive create so concurrent uploads of the same name land on distinct
files, the stream is bounded, and upload temps live in the library's
``.uploading`` subdirectory (invisible to orphan cleanup) without leftovers.
"""

import asyncio

import pytest

from app.core.exceptions import FileSystemError
from app.services import document_processing_service as dps_module
from app.services.document_processing_service import document_processing_service
from tests.factories.uploads import ChunkedUpload


async def test_lands_at_requested_name_without_temp_leftovers(tmp_path):
    target = await document_processing_service._write_upload_unique(
        tmp_path / "a.pdf", ChunkedUpload(b"hello")
    )

    assert target == tmp_path / "a.pdf"
    assert target.read_bytes() == b"hello"
    uploading_dir = tmp_path / ".uploading"
    assert uploading_dir.is_dir()
    assert list(uploading_dir.iterdir()) == []


async def test_pre_existing_name_moves_to_next_candidate(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"owned")

    target = await document_processing_service._write_upload_unique(
        tmp_path / "a.pdf", ChunkedUpload(b"new")
    )

    assert target == tmp_path / "a_1.pdf"
    assert (tmp_path / "a.pdf").read_bytes() == b"owned"
    assert target.read_bytes() == b"new"


@pytest.mark.concurrency
async def test_concurrent_same_name_uploads_land_on_distinct_files(tmp_path):
    """Two interleaved uploads of one name must never share a file_path:
    exactly one claims ``a.pdf``, the loser lands on the next candidate."""
    results = await asyncio.gather(*[
        document_processing_service._write_upload_unique(
            tmp_path / "a.pdf", ChunkedUpload(bytes([i])),
        )
        for i in range(2)
    ])

    assert len({str(r) for r in results}) == 2
    contents = sorted(r.read_bytes() for r in results)
    assert contents == [b"\x00", b"\x01"]
    assert (tmp_path / "a.pdf").read_bytes() in (b"\x00", b"\x01")


async def test_oversize_stream_claims_nothing_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(dps_module.settings.library, "upload_max_mb", 1)
    dest = tmp_path / "a.pdf"

    with pytest.raises(FileSystemError):
        await document_processing_service._write_upload_unique(
            dest, ChunkedUpload(b"x" * (2 * 1024 * 1024)),
        )

    assert not dest.exists()
    assert list((tmp_path / ".uploading").iterdir()) == []


async def test_name_clash_exhaustion_fails_and_cleans_up(tmp_path, monkeypatch):
    """When every stem_N candidate is already claimed the write gives up
    with a FileSystemError: claimed names belong to other uploads and the
    stream's temp file is removed, leaving no residue."""
    for name in ("a.pdf", "a_1.pdf", "a_2.pdf"):
        (tmp_path / name).write_bytes(b"owned")
    monkeypatch.setattr(
        dps_module.DocumentProcessingService, "MAX_NAME_CLASH_ATTEMPTS", 3,
    )

    with pytest.raises(FileSystemError):
        await document_processing_service._write_upload_unique(
            tmp_path / "a.pdf", ChunkedUpload(b"new"),
        )

    assert sorted(p.name for p in tmp_path.iterdir()) == [
        ".uploading", "a.pdf", "a_1.pdf", "a_2.pdf",
    ]
    assert list((tmp_path / ".uploading").iterdir()) == []
