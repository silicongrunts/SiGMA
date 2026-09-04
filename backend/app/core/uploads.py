"""Bounded reads and streaming writes for multipart upload endpoints."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from fastapi import UploadFile

from app.core.atomic_file import claim_path
from app.core.exceptions import FileAlreadyExistsError, FileSystemError

UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


async def read_upload_bounded(
    file: UploadFile,
    max_bytes: int,
    *,
    message: str,
    code: str,
) -> bytes:
    """Read an uploaded body in bounded chunks up to ``max_bytes``.

    Raises a 413 FileSystemError as soon as the stream exceeds the cap, so an
    oversized upload is never fully buffered in memory.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise FileSystemError(message, code=code, status_code=413)
        chunks.append(chunk)
    return b"".join(chunks)


async def write_upload_bounded(
    file: UploadFile,
    dest_path: Path,
    max_bytes: int,
    *,
    message: str,
    code: str,
    overwrite: bool = True,
) -> None:
    """Stream an uploaded body straight onto ``dest_path`` with a size cap.

    The body is written to a temp file in the destination directory in 1 MiB
    chunks; once the stream exceeds ``max_bytes`` the temp file is removed
    and a 413 FileSystemError raised. The completed upload is fsynced, then
    the destination name is claimed atomically, so no reader ever sees a
    partial file. With ``overwrite=True`` the claim is a replace; with
    ``overwrite=False`` it is an exclusive create — if the name is taken the
    existing file is untouched and ``FileAlreadyExistsError`` is raised.
    Disk writes run in a worker thread to keep the event loop responsive.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest_path.parent),
        prefix=".upload_",
        suffix=dest_path.suffix or ".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            total = 0
            while True:
                chunk = await file.read(UPLOAD_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise FileSystemError(message, code=code, status_code=413)
                await asyncio.to_thread(out.write, chunk)
            await asyncio.to_thread(out.flush)
            await asyncio.to_thread(os.fsync, out.fileno())
        if overwrite:
            await asyncio.to_thread(os.replace, tmp_path, dest_path)
        else:
            try:
                await asyncio.to_thread(claim_path, tmp_path, dest_path)
            except FileExistsError as exc:
                raise FileAlreadyExistsError(dest_path.name) from exc
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
