"""Chat image attachment file storage helpers."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.exceptions import FileSystemError
from app.core.utils import (
    detect_image_media_type as _detect_image_media_type,
    generate_id,
    image_dimensions,
    is_within,
    sanitize_filename,
    utcnow,
)
from app.core.atomic_file import atomic_write_bytes, AtomicFileExistsError
from app.core.chat_attachments import (
    ATTACHMENTS_DIR,
    MAX_CHAT_IMAGE_BYTES,
    SUPPORTED_IMAGE_MIME_TYPES,
)
from app.services.file_service import file_service


async def save_chat_image(
    project_id: str,
    filename: str,
    content: bytes,
    mime_type: str,
) -> dict[str, Any]:
    mime_type = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise FileSystemError("Unsupported image type", code="INVALID_REQUEST", status_code=422)
    if not content:
        raise FileSystemError("Image is empty", code="INVALID_REQUEST", status_code=422)
    if len(content) > MAX_CHAT_IMAGE_BYTES:
        raise FileSystemError("Image is too large", code="INVALID_REQUEST", status_code=413)

    root = settings.get_project_path(project_id)
    directory = (root / ATTACHMENTS_DIR).resolve()
    if not is_within(directory, root.resolve()):
        raise FileSystemError("Invalid attachment directory", code="PERMISSION_DENIED", status_code=403)
    directory.mkdir(parents=True, exist_ok=True)

    safe_original = sanitize_filename(filename or f"image{SUPPORTED_IMAGE_MIME_TYPES[mime_type]}")
    suffix = Path(safe_original).suffix.lower() or SUPPORTED_IMAGE_MIME_TYPES[mime_type]
    if suffix not in set(SUPPORTED_IMAGE_MIME_TYPES.values()):
        suffix = SUPPORTED_IMAGE_MIME_TYPES[mime_type]
    stem = utcnow().strftime("%Y%m%d-%H%M%S") + "-" + generate_id()[:8]
    target = directory / f"{stem}{suffix}"

    try:
        atomic_write_bytes(target, content, fail_if_exists=True)
    except AtomicFileExistsError as exc:
        raise FileSystemError("Attachment name collision", code="CONFLICT", status_code=409) from exc

    rel_path = str(target.relative_to(root))
    return {
        "path": rel_path,
        "mime_type": mime_type,
        "name": safe_original,
        "size": len(content),
    }


async def read_attachment_base64(project_id: str, path: str) -> tuple[str, str]:
    root = settings.get_project_path(project_id)
    full_path = (root / path).resolve()
    attachments_root = (root / ATTACHMENTS_DIR).resolve()
    if not is_within(full_path, attachments_root):
        raise FileSystemError("Attachment path is outside chat attachments", code="PERMISSION_DENIED", status_code=403)
    if not full_path.is_file():
        raise FileSystemError("Attachment not found", code="NOT_FOUND", status_code=404)
    data = full_path.read_bytes()
    if len(data) > MAX_CHAT_IMAGE_BYTES:
        raise FileSystemError("Attachment is too large", code="INVALID_REQUEST", status_code=413)
    mime_type = mimetypes.guess_type(full_path.name)[0] or "image/png"
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise FileSystemError("Unsupported image type", code="INVALID_REQUEST", status_code=422)
    return base64.b64encode(data).decode("ascii"), mime_type


async def read_image_path_base64(project_id: str, path: str) -> tuple[str, str]:
    """Read a project-relative or absolute image path as base64.

    This follows SiGMA's filesystem model: reads are unrestricted, while writes
    are permission-gated elsewhere. Project-relative paths are resolved inside
    the project sandbox; absolute paths are read directly.
    """
    if not path.strip():
        raise FileSystemError("Image path is required", code="INVALID_REQUEST", status_code=422)

    if path.startswith("/"):
        full_path = Path(path).resolve()
        if not full_path.is_file():
            raise FileSystemError("Image not found", code="NOT_FOUND", status_code=404)
        data = full_path.read_bytes()
        filename = full_path.name
    else:
        data = await file_service.read_file_binary(project_id, path)
        filename = Path(path).name

    if len(data) > MAX_CHAT_IMAGE_BYTES:
        raise FileSystemError("Image is too large", code="INVALID_REQUEST", status_code=413)

    mime_type = _detect_image_media_type(data)
    if mime_type is None:
        raise FileSystemError("Unsupported or invalid image type", code="INVALID_REQUEST", status_code=422)

    guessed = mimetypes.guess_type(filename)[0]
    if guessed in SUPPORTED_IMAGE_MIME_TYPES and guessed != mime_type:
        raise FileSystemError("Image file extension does not match its contents", code="INVALID_REQUEST", status_code=422)

    if mime_type != "image/webp" and image_dimensions(data) is None:
        raise FileSystemError("Cannot read image dimensions", code="INVALID_REQUEST", status_code=422)

    return base64.b64encode(data).decode("ascii"), mime_type
