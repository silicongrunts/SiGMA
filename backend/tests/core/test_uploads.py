"""Unit tests for the bounded upload write primitive."""

import pytest

from app.core.exceptions import FileAlreadyExistsError, FileSystemError
from app.core.uploads import write_upload_bounded
from tests.factories.uploads import ChunkedUpload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_write_upload_bounded_streams_to_destination(tmp_path):
    dest = tmp_path / "sub" / "data.bin"
    await write_upload_bounded(
        ChunkedUpload(b"hello world"), dest, 1024,
        message="too large", code="FILE_TOO_LARGE",
    )
    assert dest.read_bytes() == b"hello world"
    assert [p.name for p in dest.parent.iterdir()] == ["data.bin"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_write_upload_bounded_replaces_existing_destination(tmp_path):
    dest = tmp_path / "data.bin"
    dest.write_bytes(b"old")
    await write_upload_bounded(
        ChunkedUpload(b"new"), dest, 1024,
        message="too large", code="FILE_TOO_LARGE",
    )
    assert dest.read_bytes() == b"new"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_write_upload_bounded_rejects_oversize_and_cleans_up(tmp_path):
    """A body over the cap raises 413, leaves the destination untouched and
    removes the temp file."""
    dest = tmp_path / "data.bin"
    dest.write_bytes(b"precious")

    with pytest.raises(FileSystemError) as exc_info:
        await write_upload_bounded(
            ChunkedUpload(b"x" * 2049), dest, 2048,
            message="too large", code="FILE_TOO_LARGE",
        )

    assert exc_info.value.status_code == 413
    assert exc_info.value.code == "FILE_TOO_LARGE"
    assert dest.read_bytes() == b"precious"
    assert list(tmp_path.iterdir()) == [dest]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_write_upload_bounded_create_only_claims_free_name(tmp_path):
    dest = tmp_path / "data.bin"
    await write_upload_bounded(
        ChunkedUpload(b"fresh"), dest, 1024,
        message="too large", code="FILE_TOO_LARGE", overwrite=False,
    )
    assert dest.read_bytes() == b"fresh"
    assert [p.name for p in tmp_path.iterdir()] == ["data.bin"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_write_upload_bounded_create_only_never_overwrites(tmp_path):
    """With overwrite=False the claim is an exclusive create: a taken name
    raises FileAlreadyExistsError and leaves the existing content intact
    with no temp residue."""
    dest = tmp_path / "data.bin"
    dest.write_bytes(b"original")

    with pytest.raises(FileAlreadyExistsError):
        await write_upload_bounded(
            ChunkedUpload(b"loser"), dest, 1024,
            message="too large", code="FILE_TOO_LARGE", overwrite=False,
        )

    assert dest.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [dest]
