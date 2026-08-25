"""
File operation tools — read, write, edit, glob, grep, list_files.
"""

import asyncio
import base64
import fnmatch
import os
import re
import time
from pathlib import Path

from app.agents.tools.base import ToolDefinition
from app.agents.tools.formatting import format_range_footer
from app.agents.tools.registry import tool_registry
from app.agents.tools.read_state import (
    must_read_first_error,
    path_mtime,
    path_read_state_key,
    read_state_cache,
    record_path_read,
)
from app.agents.prompts import (
    PROMPT_READ, PROMPT_WRITE, PROMPT_EDIT, PROMPT_GLOB, PROMPT_GREP, PROMPT_LS,
)
from app.core.exceptions import BinaryFileError, FileMissingError, FileSystemError, ProjectNotFoundError
from app.core.utils import (
    detect_image_media_type as _detect_image_media_type,
    image_dimensions,
)
from app.core.logging import get_logger
from app.core.model_config import model_role_accepts_images
from app.core.chat_attachments import MAX_CHAT_IMAGE_BYTES, render_image_refs_tag
from app.services.file_service import (
    MAX_TOOL_READ_BYTES, check_readable, decode_text_bytes, file_service,
)

logger = get_logger(__name__)

# Soft dependency for image downsampling: images larger than the dimension
# cap are downscaled for viewing instead of rejected when Pillow is
# available (the production image ships Pillow). Without it the rejection
# error is returned.
try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in Pillow-less envs
    _PIL_AVAILABLE = False

# ── Constants ──
_DEFAULT_READ_LIMIT = 200  # Default max lines returned when no limit specified
_IMAGE_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png"))
_PDF_EXTENSIONS = frozenset((".pdf",))
_MAX_IMAGE_DIMENSION = 3840
_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _file_mtime(project_id: str, file_path: str) -> float | None:
    """Return the current mtime of *file_path*, or ``None`` if it cannot be stat'd."""
    try:
        return path_mtime(_resolve_file_path(project_id, file_path))
    except (OSError, ProjectNotFoundError, FileSystemError):
        return None


def _resolve_file_path(project_id: str, file_path: str) -> Path:
    """Resolve a tool file path using file I/O path semantics."""
    if file_path.startswith('/'):
        return Path(file_path).resolve()
    return file_service.safe_join(file_service.get_project_path(project_id), file_path)


def _read_state_key(project_id: str, file_path: str) -> str:
    """Return the canonical cache key for must-read-first state."""
    try:
        return path_read_state_key(_resolve_file_path(project_id, file_path))
    except (OSError, ProjectNotFoundError, FileSystemError):
        return file_path


def _record_read(
    project_id: str, session_id: str, file_path: str,
    content: str, is_partial: bool,
    window: tuple[int, int] | None = None,
) -> None:
    """Record a read in the per-session cache (must-read-first enforcement).

    ``window`` is the 0-indexed [start, end) line range the LLM actually saw
    (``None`` = the whole file). The cache accumulates windows across reads
    of the same file state; the edit tool refuses edits outside that
    accumulated coverage.
    """
    try:
        record_path_read(
            session_id, _resolve_file_path(project_id, file_path), content,
            is_partial, window,
        )
    except (OSError, ProjectNotFoundError, FileSystemError):
        read_state_cache.record_read(
            session_id, file_path, content, 0.0, is_partial, window,
        )


async def _read_file(
    project_id: str, session_id: str, filename: str,
    offset: int | None = None, limit: int | None = None,
    model_role: str = "",
) -> str | dict:
    """Read content from a file.

    Resolution order:
      1. Absolute path on host filesystem (read is unrestricted).
      2. Project-sandbox relative path via file_service.

    For jpg/png files, if the current model role accepts images, the binary
    content is returned as an image dict that the loop runner injects into the
    LLM context. Otherwise, binary files produce an error.

    For .pdf files, the file is converted to markdown via ``docling`` and the
    result is cached per-session; ``offset``/``limit`` apply to the converted
    text lines.
    """
    ext = Path(filename).suffix.lower()

    # ── Image path ──
    if ext in _IMAGE_EXTENSIONS:
        result = await _read_image(project_id, session_id, filename, ext)
        if not isinstance(result, dict):
            return result
        if model_role_accepts_images(model_role):
            return result
        image_ref = result.get("image_ref") or {}
        tag = render_image_refs_tag([image_ref])
        return (
            f"{result.get('text')}\n"
            "The current model cannot inspect image bytes directly. "
            "Use the vision_analyze tool with this image path when visual inspection is needed."
            f"{tag}"
        )

    # ── PDF path ──
    if ext in _PDF_EXTENSIONS:
        return await _read_pdf(project_id, session_id, filename, offset, limit)

    # ── Text path ──
    content = None

    # Route big files to the streaming range reader: the whole-file cap
    # (MAX_TOOL_READ_BYTES) no longer makes a file unreadable — only the
    # requested line window is read into memory. Small files keep the
    # whole-read path with its exact line totals.
    try:
        if filename.startswith('/'):
            big_path = Path(filename).resolve()
        else:
            big_path = _resolve_file_path(project_id, filename)
        big_stat = check_readable(big_path, filename, None)
    except FileMissingError:
        return f"Error: File not found: {filename}"
    except FileSystemError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: {exc}"

    if big_stat.st_size > MAX_TOOL_READ_BYTES:
        return await _read_file_range(
            project_id, session_id, filename, offset, limit)

    # Absolute host path: read directly. Binary errors are surfaced as-is —
    # falling through to sandbox resolution produced misleading "not found"
    # errors when the binary file actually existed on the host.
    if filename.startswith('/'):
        try:
            content = await file_service.read_file_absolute(
                filename, max_bytes=MAX_TOOL_READ_BYTES)
        except FileMissingError:
            return f"Error: File not found: {filename}"
        except BinaryFileError:
            return f"Error: File is binary: {filename}"
        except FileSystemError as exc:
            return f"Error: {exc}"

    # Relative path: resolve via project sandbox.
    if content is None:
        try:
            content = await file_service.read_file(
                project_id, filename, max_bytes=MAX_TOOL_READ_BYTES)
        except FileMissingError:
            return f"Error: File not found: {filename}"
        except BinaryFileError:
            return f"Error: File is binary: {filename}"
        except FileSystemError as exc:
            return f"Error: {exc}"
        # Empty string is valid — the file exists but is empty

    return _slice_and_record(
        project_id, session_id, filename, content, offset, limit,
    )


def _slice_and_record(
    project_id: str, session_id: str, file_path: str,
    content: str, offset: int | None, limit: int | None,
) -> str:
    """Apply offset/limit slicing and record the read in the cache.

    Output uses ``cat -n``-style line numbers (``N\\t<content>``) where ``N``
    is the file's real 1-indexed line number — so paginated reads still expose
    absolute line numbers usable for ``sigma://`` citations and edit context.

    ``offset`` is 0-indexed: ``offset=0`` (or ``None``) starts at line 1.
    ``limit=None`` or ``limit=0`` triggers the default 200-line cap. ``limit<0``
    returns the last ``abs(limit)`` lines and ignores ``offset``. Anytime the
    returned window stops short of EOF, a ``Showing lines X-Y of Z`` footer is
    appended (including when an explicit ``limit`` truncates).
    """
    if (limit is None or limit >= 0) and offset is not None and offset < 0:
        return f"Error: offset must be >= 0, got {offset}"

    is_partial = offset is not None or limit is not None
    # A trailing newline terminates the last line rather than opening a new
    # one — drop the phantom "" split() yields so totals match the streaming
    # range reader (and real editors) across the size boundary.
    lines = content.split("\n")
    total = len(lines) - 1 if content.endswith("\n") else len(lines)
    # Empty content: return "" so the LLM does not see a misleading "1\t"
    # prefix implying the file has a line.
    if not content:
        _record_read(project_id, session_id, file_path, content,
                     is_partial=False, window=(0, 0))
        return ""
    if limit is not None and limit < 0:
        start_idx = max(0, total + limit)
        end_idx = total
    else:
        start_idx = max(0, offset) if offset else 0
        if limit is None or limit == 0:
            end_idx = min(total, start_idx + _DEFAULT_READ_LIMIT)
        else:
            end_idx = min(total, start_idx + limit)

    _record_read(project_id, session_id, file_path, content,
                 is_partial=is_partial, window=(start_idx, end_idx))

    # 1-indexed line numbers (i+1) so paginated output stays absolutely located.
    numbered = [f"{i + 1}\t{lines[i]}" for i in range(start_idx, end_idx)]
    result = "\n".join(numbered)
    result += format_range_footer(
        start_idx + 1, end_idx, total, unit="lines",
    )
    return result


async def _read_file_range(
    project_id: str, session_id: str, filename: str,
    offset: int | None, limit: int | None,
) -> str:
    """Windowed read of a file larger than ``MAX_TOOL_READ_BYTES``.

    Mirrors ``_slice_and_record``'s offset/limit semantics (0-indexed offset;
    limit 0/None → default 200; negative → last abs(limit) lines) but only
    the requested window is ever read into memory.
    """
    if (limit is None or limit >= 0) and offset is not None and offset < 0:
        return f"Error: offset must be >= 0, got {offset}"

    if limit is not None and limit < 0:
        tail, count = abs(limit), None
    else:
        tail, count = None, (limit or _DEFAULT_READ_LIMIT)

    try:
        if filename.startswith('/'):
            r = await file_service.read_text_range_absolute(
                filename, offset=offset or 0, limit=count, tail=tail)
        else:
            r = await file_service.read_text_range(
                project_id, filename, offset=offset or 0, limit=count, tail=tail)
    except FileMissingError:
        return f"Error: File not found: {filename}"
    except BinaryFileError:
        return f"Error: File is binary: {filename}"
    except FileSystemError as exc:
        return f"Error: {exc}"

    _record_read(project_id, session_id, filename, "\n".join(r.lines),
                 is_partial=True, window=(r.start_idx, r.end_idx))

    numbered = [f"{r.start_idx + i + 1}\t{line}" for i, line in enumerate(r.lines)]
    result = "\n".join(numbered)
    if r.total_lines is None:
        # Scan budget hit before EOF: the window is valid but the total is
        # not — say so instead of inventing one.
        if r.lines:
            result += (
                f"\n\n... (showing lines {r.start_idx + 1}-{r.end_idx}; file "
                "too large to count the remaining lines — continue with "
                f"offset={r.end_idx})"
            )
    else:
        result += format_range_footer(
            r.start_idx + 1, r.end_idx, r.total_lines, unit="lines")
    return result


async def _read_pdf(
    project_id: str, session_id: str, filename: str,
    offset: int | None, limit: int | None,
) -> str:
    """Convert a PDF to markdown via docling and slice the result.

    Conversion is slow (seconds per page), so the converted text is cached on
    the per-session read-state entry. Subsequent reads of the same PDF reuse
    the cached markdown as long as the file's mtime has not changed.
    """
    # Verify the file exists before attempting conversion
    try:
        if filename.startswith('/'):
            if not Path(filename).is_file():
                return f"Error: File not found: {filename}"
        else:
            # read_file_binary enforces sandbox containment
            await file_service.read_file_binary(project_id, filename)
    except FileMissingError:
        return f"Error: File not found: {filename}"
    except FileSystemError as exc:
        return f"Error: {exc}"

    # Cache lookup: reuse converted markdown if file mtime matches
    current_mtime = _file_mtime(project_id, filename)
    cached = read_state_cache.get(session_id, _read_state_key(project_id, filename))
    if cached and cached.mtime == (current_mtime if current_mtime is not None else 0.0) and cached.content:
        return _slice_and_record(
            project_id, session_id, filename, cached.content, offset, limit,
        )

    # Convert via docling (heavy — runs in a thread executor)
    try:
        from docling.document_converter import DocumentConverter
    except ImportError:
        return (
            "Error: PDF support requires the 'docling' package, which is not installed. "
            "Install it with: pip install docling"
        )

    abs_path = filename if filename.startswith('/') else str(
        file_service.get_project_path(project_id) / filename
    )

    def _convert() -> str:
        converter = DocumentConverter()
        result = converter.convert(abs_path)
        return result.document.export_to_markdown()

    try:
        markdown = await asyncio.to_thread(_convert)
    except Exception as exc:
        logger.exception("PDF conversion failed for %s", filename)
        return f"Error: Failed to convert PDF {filename}: {exc}"

    return _slice_and_record(
        project_id, session_id, filename, markdown, offset, limit,
    )


async def _read_image(
    project_id: str, session_id: str, filename: str, ext: str,
) -> str | dict:
    """Read an image file and return an image dict for the loop runner.

    Returns a string error message on failure, or a dict with type="image"
    on success.  The loop runner's ``_normalize_tool_result`` converts the
    dict into an ephemeral multimodal message injected into the LLM context.
    """
    # Read raw bytes — absolute or sandbox path. stat-first: the byte cap
    # and regular-file check run before any byte is read, so an oversized
    # "image" or a device file never enters memory.
    try:
        if filename.startswith('/'):
            p = Path(filename).resolve()
            check_readable(p, filename, MAX_CHAT_IMAGE_BYTES)
            raw = await asyncio.to_thread(p.read_bytes)
        else:
            raw = await file_service.read_file_binary(
                project_id, filename, max_bytes=MAX_CHAT_IMAGE_BYTES)
    except FileMissingError:
        return f"Error: File not found: {filename}"
    except FileSystemError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: Unable to read image file {filename}: {exc}"

    media_type = _detect_image_media_type(raw)
    expected_media_type = _IMAGE_MEDIA_TYPES.get(ext)
    if media_type is None:
        return f"Error: Unsupported or invalid image file: {filename}. Only PNG and JPG images are supported."
    if expected_media_type and media_type != expected_media_type:
        return (
            f"Error: Image file extension does not match its contents: {filename}. "
            f"Expected {expected_media_type}, detected {media_type}."
        )

    # Validate image dimensions from binary header
    dims = image_dimensions(raw)
    if dims is None:
        return f"Error: Cannot read image dimensions from {filename}. The file may be corrupted or not a valid image."
    w, h = dims
    note = ""
    if w > _MAX_IMAGE_DIMENSION or h > _MAX_IMAGE_DIMENSION:
        if not _PIL_AVAILABLE:
            return (
                f"Error: Image resolution {w}\u00d7{h} exceeds the "
                f"{_MAX_IMAGE_DIMENSION}\u00d7{_MAX_IMAGE_DIMENSION} limit. "
                "Please resize the image."
            )
        try:
            raw, scaled = await asyncio.to_thread(
                _downscale_image, raw, w, h, media_type)
            # Header shows the final size; the note carries the original.
            note = f" (downscaled from {w}\u00d7{h} for viewing)"
            w, h = scaled
        except Exception:
            logger.warning("image downscale failed for %s", filename,
                           exc_info=True)
            return (
                f"Error: Image resolution {dims[0]}\u00d7{dims[1]} exceeds "
                f"the {_MAX_IMAGE_DIMENSION}\u00d7{_MAX_IMAGE_DIMENSION} "
                "limit and could not be downscaled. Please resize the image."
            )

    # Images are read as one unit — no offset/limit applies.
    _record_read(project_id, session_id, filename, content="", is_partial=False)

    image_base64 = await asyncio.to_thread(
        lambda: base64.b64encode(raw).decode("ascii"))
    text = f"Image file: {filename} ({w}\u00d7{h}){note}"
    return {
        "type": "image",
        "image_base64": image_base64,
        "media_type": media_type,
        "text": text,
        "image_ref": {
            "path": filename,
            "mime_type": media_type,
            "name": Path(filename).name,
            "source": "read",
            "text": text,
        },
    }


def _downscale_image(raw: bytes, w: int, h: int, media_type: str):
    """Resize an over-dimension image to fit the cap; returns (bytes, (w, h)).

    Runs in a worker thread via ``asyncio.to_thread`` — decode/resize is
    pure CPU and must stay off the event loop.
    """
    import io

    img = _PILImage.open(io.BytesIO(raw))
    img.load()
    scale = _MAX_IMAGE_DIMENSION / max(w, h)
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    img = img.resize(new_size, _PILImage.LANCZOS)
    buf = io.BytesIO()
    if media_type == "image/jpeg":
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=85)
    else:
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGBA")
        img.save(buf, format="PNG")
    return buf.getvalue(), img.size


async def _write_file(
    project_id: str, session_id: str, file_path: str, content: str,
) -> str:
    """Write content to a file.

    Routes to ``file_service`` based on absolute vs relative path. Permission
    is checked by ``permission_executor._check_write`` before this runs.

    Must-read-first: existing files must have been read earlier in this
    conversation, including paginated reads (a compaction resets the cache —
    re-read after compact). Stale reads (file modified on disk since read) are
    rejected.
    """
    err = _must_read_first_error_for(project_id, session_id, file_path, op="write")
    if err:
        return err

    if file_path.startswith('/'):
        await file_service.write_file_absolute(project_id, file_path, content)
    else:
        await file_service.write_file(project_id, file_path, content)

    # Refresh the cache so subsequent edits/writes in the same turn pass the
    # staleness check without forcing a re-read.
    _record_read(project_id, session_id, file_path, content, is_partial=False)
    return f"File written: {file_path} ({len(content)} chars)"


async def _edit_file(
    project_id: str, session_id: str, file_path: str,
    old_string: str, new_string: str, replace_all: bool = False,
) -> str:
    """Perform exact string replacements in an existing file.

    Uses exact string matching — if ``old_string`` is not unique, the edit
    fails unless ``replace_all=True``.

    Must-read-first: like ``_write_file``, requires a prior read in this
    conversation segment, and the read must cover the region being edited
    (``replace_all`` additionally requires a whole-file read).

    Line-ending/encoding robustness: matching happens on CRLF-normalized
    text so ``old_string`` copied from read output always matches, and the
    write-back preserves the file's dominant line endings, BOM, and
    encoding (UTF-8 / BOM'd UTF-16).
    """
    if old_string == new_string:
        return "Error: old_string and new_string are identical. No changes needed."

    if file_path.lower().endswith(".ipynb"):
        return ("Error: file is a Jupyter notebook — use the notebook_edit "
                "tool to edit cells.")

    if not old_string:
        return ("Error: old_string must be non-empty. Use the write tool to "
                "create a file or replace its whole content.")

    err = _must_read_first_error_for(project_id, session_id, file_path, op="edit")
    if err:
        return err

    # Read the raw bytes: matching needs CRLF normalization and the write
    # -back needs the original encoding/BOM. The whole file must fit in
    # memory for exact-match replacement, so the whole-read cap applies;
    # oversized files get a targeted-edit suggestion.
    if file_path.startswith('/'):
        try:
            raw = await file_service.read_file_absolute_bytes(
                file_path, max_bytes=MAX_TOOL_READ_BYTES)
        except FileMissingError:
            return f"Error: File not found: {file_path}"
        except FileSystemError as exc:
            if exc.code == "FILE_TOO_LARGE":
                return f"Error: {exc} (bash `sed -i` handles targeted edits on large files)"
            return f"Error: {exc}"
    else:
        try:
            raw = await file_service.read_file_binary(
                project_id, file_path, max_bytes=MAX_TOOL_READ_BYTES)
        except FileMissingError:
            return f"Error: File not found: {file_path}"
        except FileSystemError as exc:
            if exc.code == "FILE_TOO_LARGE":
                return f"Error: {exc} (bash `sed -i` handles targeted edits on large files)"
            return f"Error: {exc}"

    info = decode_text_bytes(raw)
    if info.encoding == "utf-8" and b"\x00" in raw[:8192]:
        return f"Error: File is binary: {file_path}"

    # CRLF is invisible in read output — normalize everything, restore the
    # dominant ending on write-back below.
    content = info.text.replace("\r\n", "\n")
    old_string = old_string.replace("\r\n", "\n")
    new_string = new_string.replace("\r\n", "\n")

    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {file_path}"
    if count > 1 and not replace_all:
        return f"Error: old_string appears {count} times in {file_path}. Use replace_all=true or provide more context."

    # Region gate: refuse edits to lines the model has not read. Blind edits
    # on huge files are how stale-assumption bugs sneak in. Any read window
    # recorded against the current file state counts (read output defaults
    # to a 200-line window, so a large file may need several windowed reads;
    # coverage resets when the file changes on disk).
    entry = read_state_cache.get(
        session_id, _read_state_key(project_id, file_path))
    if entry is not None and entry.coverage is not None:
        read_desc = ", ".join(f"{s + 1}-{e}" for s, e in entry.coverage)
        # Same trailing-newline normalization as _slice_and_record's totals.
        total_lines = content.count("\n") + (0 if content.endswith("\n") else 1)
        full_read = (len(entry.coverage) == 1
                     and entry.coverage[0][0] == 0
                     and entry.coverage[0][1] >= total_lines)
        if replace_all and not full_read:
            return (
                f"Error: replace_all requires reading the whole file first "
                f"(lines {read_desc} of {total_lines} "
                "were read). Re-read with a larger limit, or edit each "
                "occurrence individually."
            )
        if not replace_all:
            pos = content.find(old_string)
            line_no = content.count("\n", 0, pos)
            if not any(s <= line_no < e for s, e in entry.coverage):
                return (
                    f"Error: old_string is at line {line_no + 1}, outside "
                    f"the lines read ({read_desc}). Read that region "
                    f"first, e.g. read with offset={max(0, line_no - 20)}."
                )

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    # Restore dominant line endings and the original encoding/BOM.
    if info.crlf:
        new_content = new_content.replace("\n", "\r\n")
    encoding = info.encoding
    if info.had_bom:
        if encoding == "utf-8":
            # The utf-8-sig codec prepends the BOM itself.
            encoding = "utf-8-sig"
        else:
            # utf-16-le/-be codecs write no BOM — prepend it as a character
            # and keep the file's original byte order (the plain "utf-16"
            # codec would silently flip it to the machine's native order).
            new_content = "\ufeff" + new_content

    # Write back — permission already checked by permission_executor._check_write
    if file_path.startswith('/'):
        await file_service.write_file_absolute(
            project_id, file_path, new_content, encoding=encoding)
    else:
        await file_service.write_file(
            project_id, file_path, new_content, encoding=encoding)

    # Refresh cache so further edits in the same turn pass the staleness check.
    _record_read(project_id, session_id, file_path, new_content, is_partial=False)
    return f"File edited: {file_path} ({count} replacement(s))"


def _must_read_first_error_for(
    project_id: str, session_id: str, file_path: str, *, op: str,
) -> str | None:
    """Resolve *file_path* and return the standard must-read-first error, if any.

    Thin wrapper over the shared ``must_read_first_error`` helper that handles
    the tool's path-resolution failure modes — when the path cannot be resolved
    (project missing, unreadable dir), there is no meaningful must-read state,
    so no error is surfaced and the tool's own later logic handles the failure.
    """
    try:
        resolved = _resolve_file_path(project_id, file_path)
    except (OSError, ProjectNotFoundError, FileSystemError):
        return None
    return must_read_first_error(session_id, resolved, op=op)


# list_files caps every listing mode (single directory or rendered tree);
# larger listings report the remainder instead of flooding the model context.
_MAX_LIST_ENTRIES = 2000


async def _ls_scan(target: Path, label: str) -> str:
    """List *target*'s children (dirs first, hidden filtered) in a worker
    thread, capped at ``_MAX_LIST_ENTRIES`` entries."""

    def _scan() -> tuple[list[str], int]:
        entries = sorted(
            target.iterdir(),
            key=lambda x: (not x.is_dir(), x.name.lower()),
        )
        lines = [
            f"{e.name}{'/' if e.is_dir() else ''}"
            for e in entries
            if not e.name.startswith('.')
        ]
        shown = lines[:_MAX_LIST_ENTRIES]
        return shown, len(lines) - len(shown)

    try:
        lines, more = await asyncio.to_thread(_scan)
    except FileNotFoundError:
        return f"Directory not found: {label}"
    except NotADirectoryError:
        return f"Not a directory: {label}"
    if not lines:
        return "(empty directory)"
    if more:
        lines.append(f"... +{more} more entries not shown (use a more specific path)")
    return "\n".join(lines)


async def _list_files(project_id: str, dirname: str = "") -> str:
    """List files in the project directory."""
    try:
        # Absolute path — browse host filesystem directly (LS is read-only)
        if dirname and os.path.isabs(dirname):
            target = Path(dirname).resolve()
            if not target.exists():
                return f"Directory not found: {dirname}"
            if not target.is_dir():
                return f"Not a directory: {dirname}"
            return await _ls_scan(target, dirname)

        # Relative path — browse project sandbox (safe_join keeps containment)
        if dirname:
            target = file_service.safe_join(
                file_service.get_project_path(project_id), dirname)
            return await _ls_scan(target, dirname)

        # Project root tree
        tree = await file_service.get_project_tree(project_id)
        def _fmt(node: dict, prefix: str = "") -> list[str]:
            lines = []
            for child in node.get("children", []):
                suffix = "/" if child["type"] == "directory" else ""
                lines.append(f"{prefix}{child['name']}{suffix}")
                if child["type"] == "directory":
                    lines.extend(_fmt(child, prefix + "  "))
            return lines
        lines = _fmt(tree.get("root", {}))
        if not lines:
            return "(empty project)"
        if len(lines) > _MAX_LIST_ENTRIES:
            more = len(lines) - _MAX_LIST_ENTRIES
            lines = lines[:_MAX_LIST_ENTRIES]
            lines.append(
                f"... +{more} more entries not shown (use a more specific path)")
        return "\n".join(lines)
    except FileMissingError:
        return f"Directory not found: {dirname}"
    except FileSystemError as e:
        return str(e.message) if hasattr(e, "message") else str(e)
    except ProjectNotFoundError:
        return f"Project not found: {project_id}"
    except Exception as e:
        logger.exception("list_files failed")
        return f"Error: {e}"


def _expand_braces(pattern: str) -> list[str]:
    """Expand brace patterns like *.{txt,py,js} into individual patterns.

    Python's glob module doesn't handle brace expansion natively.
    """
    m = re.search(r'\{([^{}]+)\}', pattern)
    if not m:
        return [pattern]
    prefix = pattern[:m.start()]
    suffix = pattern[m.end():]
    results = []
    for alt in m.group(1).split(','):
        results.extend(_expand_braces(prefix + alt.strip() + suffix))
    return results


_GLOB_MAX_RESULTS = 100

# Bounded-search budgets. The glob/grep subprocesses are the traversal
# engine: they run in a killable process (a walk can never wedge the worker's
# event loop), with a wall-clock deadline and an output-size cap. 20 MB of
# paths is already pathological (~200k entries) and stops the collection
# before sorting/stat-ing floods memory.
_GLOB_TIMEOUT_SECONDS = 20
_SEARCH_MAX_OUTPUT_BYTES = 20 * 1024 * 1024
_SEARCH_KILL_GRACE_SEC = 5.0
_GLOB_MAX_MATCHES = 10_000
# Fallback (rg missing) walks in-process; it must be independently bounded.
_GLOB_FALLBACK_DEADLINE_SEC = 10.0
_GLOB_FALLBACK_MAX_MATCHES = 1_000


class SearchProcessError(Exception):
    """The search binary exited with a real error (not 0/1 = match/no-match).

    Without this classification a bad regex, a missing path, or a resource
    error is indistinguishable from "no results" — the LLM would reason on a
    false premise instead of fixing its input.
    """

    def __init__(self, returncode: int | None, stderr: bytes):
        lines = [l for l in
                 stderr.decode("utf-8", errors="replace").strip().splitlines()
                 if l.strip()]
        message = lines[0] if lines else f"exited with code {returncode}"
        # rg splits regex diagnostics across lines ("regex parse error:" …
        # "error: unclosed character class"); surface the summary line too.
        for later in lines[1:6]:
            if later.strip().startswith("error:"):
                message = f"{message} {later.strip()}"
                break
        super().__init__(message)
        self.returncode = returncode


class _SearchRun:
    """Outcome of one bounded search subprocess run."""

    __slots__ = ("stdout", "stderr", "returncode", "bound_exceeded")

    def __init__(self, stdout: bytes, stderr: bytes,
                 returncode: int | None, bound_exceeded: bool):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.bound_exceeded = bound_exceeded


def _is_eagain(stderr: bytes) -> bool:
    """rg failed to spawn its thread pool (Docker/CI ulimits, os error 11)."""
    return (b"os error 11" in stderr
            or b"Resource temporarily unavailable" in stderr)


async def _run_bounded_search(
    cmd: list[str], *, cwd: str | None, timeout: float,
) -> _SearchRun:
    """Run a search subprocess under a hard wall-clock + output budget.

    Returns the collected stdout plus the process's stderr and exit code so
    callers can tell "no matches" (0/1) from failure (anything else) — a
    classification the raw byte stream cannot provide. On timeout or
    output-cap the process is SIGKILLed (rg keeps no state worth a graceful
    SIGTERM) and whatever stdout was collected is returned so callers can
    surface partial results. stdout is read incrementally in bounded chunks;
    stderr is drained by a concurrent task (a full stderr pipe would
    deadlock the stdout loop).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    stderr_task = asyncio.create_task(proc.stderr.read())
    chunks: list[bytes] = []
    total = 0
    bound_exceeded = False
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                bound_exceeded = True
                break
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(65536), timeout=remaining)
            except asyncio.TimeoutError:
                bound_exceeded = True
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total >= _SEARCH_MAX_OUTPUT_BYTES:
                bound_exceeded = True
                break
    finally:
        if proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_SEARCH_KILL_GRACE_SEC)
        except asyncio.TimeoutError:
            logger.warning("search subprocess did not exit after SIGKILL: %s",
                           cmd[:2])
        stderr = b""
        try:
            stderr = await asyncio.wait_for(
                stderr_task, timeout=_SEARCH_KILL_GRACE_SEC)
        except Exception:
            stderr_task.cancel()
        _close_search_pipes(proc)
    return _SearchRun(b"".join(chunks), stderr or b"",
                      proc.returncode, bound_exceeded)


async def _run_rg_search(
    cmd: list[str], *, cwd: str | None, timeout: float,
) -> _SearchRun:
    """``_run_bounded_search`` for rg commands, with the EAGAIN retry.

    In containers with low thread limits rg can fail to spawn its worker
    pool ("Resource temporarily unavailable"). One retry in single-threaded
    mode (``-j 1``) fixes it; the flag applies to that call only, never
    globally — persistent single-threading slows large-repo searches.
    """
    run = await _run_bounded_search(cmd, cwd=cwd, timeout=timeout)
    if (run.returncode not in (0, 1) and not run.bound_exceeded
            and _is_eagain(run.stderr)):
        logger.info("rg hit EAGAIN; retrying single-threaded (-j 1)")
        run = await _run_bounded_search(
            [cmd[0], "-j", "1", *cmd[1:]], cwd=cwd, timeout=timeout)
    return run


def _close_search_pipes(proc: asyncio.subprocess.Process) -> None:
    """Release the stdout/stderr pipe transports after the bounded-read loop.

    ``proc.communicate()`` normally closes transports; this loop reads
    incrementally and may abort before EOF, so close explicitly.
    """
    for stream in (proc.stdout, proc.stderr):
        transport = getattr(stream, "_transport", None)
        if transport is not None and not transport.is_closing():
            transport.close()

# Virtual or self-referential filesystem roots. A recursive walk from / or
# /proc never terminates (/proc/<pid>/root is a symlink back to /), so glob
# and grep refuse these roots outright instead of relying on the timeout to
# kill the walk.
_VIRTUAL_FS_ROOTS = ("/proc", "/sys", "/dev", "/run")


def _is_virtual_fs_root(root: Path) -> bool:
    """True when *root* is ``/`` itself or one of the virtual FS trees."""
    if str(root) == "/":
        return True
    return any(root == Path(v) or Path(v) in root.parents for v in _VIRTUAL_FS_ROOTS)


def _virtual_root_refusal(project_id: str, root) -> str:
    hint = ""
    try:
        if project_id:
            hint = f", e.g. {file_service.get_project_path(project_id)}"
    except Exception:
        pass
    return (
        f"Error: refusing to search from '{root}' — virtual filesystem roots "
        f"never finish walking. Search a concrete directory instead{hint}."
    )


def _split_absolute_pattern(pattern: str) -> tuple[Path, str]:
    """Split an absolute glob pattern into a literal base dir + relative glob.

    ``/etc/**/*.conf`` → ``/etc`` + ``**/*.conf``. The final segment always
    stays in the pattern (it matches literally against itself), so
    ``/etc/hosts`` → ``/etc`` + ``hosts``. A pattern whose first segment is
    already a wildcard has no literal base and lands on ``/`` — which the
    caller refuses.
    """
    parts = pattern.lstrip("/").split("/")
    base_parts: list[str] = []
    for seg in parts[:-1]:
        if any(c in seg for c in "*?[{"):
            break
        base_parts.append(seg)
    base = Path("/" + "/".join(base_parts)) if base_parts else Path("/")
    rel = "/".join(parts[len(base_parts):])
    return base, rel


def _translate_glob_for_rg(pattern: str) -> str:
    """Adapt a Python-glob pattern to ripgrep ``--glob`` semantics.

    - Patterns without ``/`` match only the top level in Python glob, but a
      bare rg glob matches basenames at any depth — anchor with a leading ``/``.
    - A pattern segment that explicitly starts with ``.`` must be allowed to
      match hidden entries (rg skips them unless ``--hidden``; the flag is
      added by the caller in that case).
    """
    if "/" not in pattern:
        return "/" + pattern
    return pattern


async def _glob_search(project_id: str = "", pattern: str = "", path: str = ".") -> str:
    """Find files matching a glob pattern.

    Results are sorted by modification time (newest first), with alphabetical
    order as a tiebreaker. When ``path`` is a relative subdirectory, returned
    paths are still relative to the project root (so the LLM can pass them
    directly to ``read``); when ``path`` is absolute, returned paths are
    absolute.

    The walk runs in a ripgrep subprocess under a wall-clock deadline and an
    output cap, so no pattern — however broad — can wedge the worker's event
    loop. Virtual filesystem roots are refused outright; rg additionally
    never follows directory symlinks.
    """
    if not (pattern or "").strip():
        return "Error: pattern is required"

    # Absolute patterns carry their own search root (`/etc/**/*.conf`).
    if pattern.startswith("/"):
        base, rel = _split_absolute_pattern(pattern)
        if _is_virtual_fs_root(base):
            return _virtual_root_refusal(project_id, base)
        search_dir, sub_patterns, path_is_absolute = base, _expand_braces(rel), True
    else:
        search_dir = Path(path if os.path.isabs(path) else os.path.join(
            str(file_service.get_project_path(project_id)) if project_id else ".", path,
        ))
        if _is_virtual_fs_root(search_dir.resolve()):
            return _virtual_root_refusal(project_id, search_dir)
        sub_patterns, path_is_absolute = _expand_braces(pattern), os.path.isabs(path)

    # Existence up front: a missing directory must be an error, not a
    # silent "no files matching".
    if not search_dir.is_dir():
        return f"Error: Directory does not exist: {search_dir}"

    # ``path`` relative subdirectory (e.g. "src"): rg echoes paths relative to
    # the search dir ("foo.ts"); prepend the subdirectory so the LLM sees
    # project-relative paths ("src/foo.ts").
    rel_prefix = "" if (path_is_absolute or path in (".", "", "./")) \
        else path.rstrip("/") + "/"

    try:
        raw, bound_exceeded, match_capped = await _glob_via_rg(
            search_dir, sub_patterns)
    except FileNotFoundError:
        raw, bound_exceeded = await asyncio.to_thread(
            _glob_fallback_walk, search_dir, sub_patterns)
        # The fallback walk genuinely stops at its own match cap, so its
        # budget flag already covers that case.
        match_capped = False
    except SearchProcessError as exc:
        return f"Error: glob search failed: {exc}"

    seen: set[str] = set()
    matches: list[str] = []
    for m in raw:
        # Both engines (rg and the fallback walker) return paths relative to
        # search_dir; rejoin for absolute-output searches.
        full = (os.path.normpath(os.path.join(str(search_dir), m))
                if path_is_absolute else rel_prefix + m)
        if full not in seen:
            seen.add(full)
            matches.append(full)

    if not matches:
        if bound_exceeded:
            return (f"Error: glob for '{pattern}' exceeded its search budget "
                    "without finding anything. Use a more specific path or "
                    "pattern.")
        return f"No files matching '{pattern}'"

    matches = await asyncio.to_thread(
        _sort_by_mtime, matches, path_is_absolute, project_id)

    shown = matches[:_GLOB_MAX_RESULTS]
    result = "\n".join(shown)
    notes = []
    if len(matches) > len(shown):
        notes.append(f"{len(matches) - len(shown)} more matches not shown")
    if match_capped:
        # The search itself finished; only the collected list was truncated.
        notes.append(f"results capped at {_GLOB_MAX_MATCHES:,} matches; "
                     "use a more specific path or pattern")
    if bound_exceeded:
        notes.append("search stopped early — time or output budget exceeded; "
                     "use a more specific path or pattern")
    if notes:
        # Leading "\n\n" matches the read truncation suffix style.
        result += "\n\n... (" + "; ".join(notes) + ")"
    return result


async def _glob_via_rg(search_dir, sub_patterns: list[str]) -> tuple[list[str], bool, bool]:
    """Run ``rg --files`` for the patterns; returns (relative paths,
    budget_exceeded, match_capped).

    Paths come back relative to *search_dir* (no ``./`` prefix); callers
    rejoin the prefix for absolute-output searches. The process always runs
    with ``cwd=search_dir`` and a ``.`` argument: rg resolves a leading-``/``
    glob (see ``_translate_glob_for_rg``) against the *cwd*, so passing an
    absolute search argument instead would anchor against the wrong root and
    silently match nothing. ``-0`` (NUL-separated) is the only path-safe
    framing: paths may contain newlines.
    """
    cmd = ["rg", "--files", "-0", "--no-ignore"]
    # rg skips hidden entries by default, mirroring Python glob; explicit
    # dot-prefixed pattern segments opt in (same rule as the fallback
    # walker's `want` below).
    if any(seg.startswith(".") for p in sub_patterns for seg in p.split("/")):
        cmd.append("--hidden")
    for p in sub_patterns:
        cmd += ["--glob", _translate_glob_for_rg(p)]
    cmd.append(".")

    run = await _run_rg_search(
        cmd, cwd=str(search_dir),
        timeout=_GLOB_TIMEOUT_SECONDS)
    # Exit 0/1 = matched/no-match. Anything else with no output is a real
    # failure (bad pattern, resource error) — raising lets the caller report
    # it instead of a false "No files matching"; partial output still wins.
    # A killed run (timeout/output cap) is a budget report, not an error.
    if (not run.bound_exceeded and run.returncode not in (0, 1)
            and not run.stdout):
        raise SearchProcessError(run.returncode, run.stderr)
    entries = [e.decode("utf-8", errors="replace")
               for e in run.stdout.split(b"\0") if e]
    # rg echoes "./name" for a "." search; callers expect paths relative to
    # the search dir without the prefix.
    entries = [e[2:] if e.startswith("./") else e for e in entries]
    if run.bound_exceeded and entries:
        # The process was killed mid-stream: the last entry may be a partial
        # path, so drop it rather than return a corrupt name.
        entries.pop()
    match_capped = len(entries) > _GLOB_MAX_MATCHES
    return entries[:_GLOB_MAX_MATCHES], run.bound_exceeded, match_capped


def _glob_fallback_walk(search_dir, sub_patterns: list[str]) -> tuple[list[str], bool]:
    """In-process bounded glob, used only when the rg binary is missing.

    Follows Python glob segment semantics but never descends into symlinked
    directories — the self-reference hazard rg already avoids — and enforces
    a deadline plus a match cap. Returns (relative paths, budget_exceeded).
    """
    deadline = time.monotonic() + _GLOB_FALLBACK_DEADLINE_SEC
    matches: list[str] = []
    exceeded = [False]

    def want(name: str, seg: str) -> bool:
        # Python glob: '*' does not match leading-dot names unless the
        # pattern segment itself starts with a dot.
        if not seg.startswith(".") and name.startswith("."):
            return False
        return fnmatch.fnmatchcase(name, seg)

    def walk(dir_path: Path, segs: tuple[str, ...], rel: str) -> None:
        if exceeded[0] or len(matches) >= _GLOB_FALLBACK_MAX_MATCHES:
            exceeded[0] = True
            return
        if time.monotonic() > deadline:
            exceeded[0] = True
            return
        try:
            entries = list(os.scandir(dir_path))
        except OSError:
            return
        if not segs:
            return
        seg, rest = segs[0], segs[1:]
        allow_hidden = any(s.startswith(".") for s in segs)
        if seg == "**":
            if not rest:
                # A trailing '**' matches everything below this point.
                walk_all(dir_path, rel, allow_hidden)
                return
            walk(dir_path, rest, rel)  # '**' may match zero directories
            for e in entries:
                if not e.is_dir(follow_symlinks=False):
                    continue
                if e.name.startswith(".") and not allow_hidden:
                    continue
                walk(Path(e.path), segs, _join_rel(rel, e.name))
        elif not rest:
            for e in entries:
                if want(e.name, seg):
                    matches.append(_join_rel(rel, e.name))
        else:
            for e in entries:
                if e.is_dir(follow_symlinks=False) and want(e.name, seg):
                    walk(Path(e.path), rest, _join_rel(rel, e.name))

    def walk_all(dir_path: Path, rel: str, allow_hidden: bool) -> None:
        if exceeded[0] or len(matches) >= _GLOB_FALLBACK_MAX_MATCHES:
            exceeded[0] = True
            return
        if time.monotonic() > deadline:
            exceeded[0] = True
            return
        try:
            entries = list(os.scandir(dir_path))
        except OSError:
            return
        for e in entries:
            # Same hidden-entry rule as `want`/the '**' descent above.
            if e.name.startswith(".") and not allow_hidden:
                continue
            matches.append(_join_rel(rel, e.name))
            if e.is_dir(follow_symlinks=False):
                walk_all(Path(e.path), _join_rel(rel, e.name), allow_hidden)

    for p in sub_patterns:
        walk(Path(search_dir), tuple(s for s in p.split("/") if s), "")
    return matches, exceeded[0]


def _join_rel(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def _sort_by_mtime(matches: list[str], path_is_absolute: bool,
                   project_id: str) -> list[str]:
    """Sort by mtime desc, then alphabetically. Best-effort: stat failures
    fall back to mtime=0 (oldest)."""
    def _mtime_key(p: str) -> tuple[float, str]:
        try:
            if path_is_absolute:
                return (-Path(p).stat().st_mtime, p)
            base = file_service.get_project_path(project_id) if project_id else Path(".")
            return (-(base / p).stat().st_mtime, p)
        except OSError:
            return (0.0, p)
    return sorted(matches, key=_mtime_key)


# ── grep (ripgrep-backed content search) ────────────────────────────

_GREP_DEFAULT_HEAD_LIMIT = 250
_GREP_TIMEOUT_SECONDS = 15


def _build_rg_command(
    pattern: str,
    search_path: str,
    *,
    output_mode: str,
    glob_filter: str,
    type_filter: str,
    case_insensitive: bool,
    line_numbers: bool,
    after_context: int,
    before_context: int,
    context: int,
    multiline: bool,
) -> list[str]:
    """Build the ripgrep argv for the given parameters.

    ``pattern`` is passed via ``-e`` so patterns starting with ``-`` are not
    parsed as flags.
    """
    cmd: list[str] = ["rg", "--no-heading"]
    # Long lines (minified JS, base64 blobs) are omitted instead of flooding
    # the tool result; rg prints "[Omitted long matching line]".
    cmd.extend(["--max-columns", "500"])
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("-c")
    else:  # content
        if line_numbers:
            cmd.append("-n")
    if case_insensitive:
        cmd.append("-i")
    if multiline:
        cmd.extend(["-U", "--multiline-dotall"])
    # Context wins over -A/-B if both are supplied.
    if context and context > 0:
        cmd.extend(["-C", str(context)])
    else:
        if after_context and after_context > 0:
            cmd.extend(["-A", str(after_context)])
        if before_context and before_context > 0:
            cmd.extend(["-B", str(before_context)])
    if glob_filter:
        cmd.extend(["--glob", glob_filter])
    if type_filter:
        cmd.extend(["--type", type_filter])
    # ``-e`` accepts the pattern as a single argument even if it starts with -
    cmd.extend(["-e", pattern, search_path])
    return cmd


def _format_grep_output(
    raw: str,
    *,
    head_limit: int,
    offset: int,
) -> str:
    """Apply offset/head_limit pagination and append a marker if truncated."""
    if offset < 0:
        return f"Error: offset must be >= 0, got {offset}"
    if not raw:
        return ""

    all_lines = raw.split("\n")
    total = len(all_lines)

    if head_limit and head_limit > 0:
        end = min(total, offset + head_limit)
    else:
        end = total
    shown = all_lines[offset:end]

    result = "\n".join(shown)
    if head_limit and head_limit > 0 and end < total:
        # 1-indexed result window: offset..end-1 in 0-indexed terms.
        result += format_range_footer(offset + 1, end, total, unit="results")
    return result


async def _grep_search(
    project_id: str = "",
    pattern: str = "",
    path: str = ".",
    glob_filter: str = "",
    output_mode: str = "files_with_matches",
    type_filter: str = "",
    flags: dict | None = None,
    context: int | None = None,
    head_limit: int = _GREP_DEFAULT_HEAD_LIMIT,
    offset: int = 0,
    multiline: bool = False,
) -> str:
    """Search file contents using ripgrep (with grep fallback).

    ``flags`` collects the hyphenated parameters
    (``-i``/``-n``/``-A``/``-B``/``-C``) that cannot be expressed as Python
    identifiers.
    """
    flags = flags or {}
    case_insensitive = bool(flags.get("-i", False))
    line_numbers = bool(flags.get("-n", True))
    after_context = int(flags.get("-A") or 0)
    before_context = int(flags.get("-B") or 0)
    flag_c = flags.get("-C")
    # ``context`` wins over ``-C`` if both supplied
    effective_context = context if context else (int(flag_c) if flag_c else 0)

    # Run rg/grep with cwd=project root and a relative search path so they
    # echo project-relative paths (glob does the same); an absolute `path`
    # keeps absolute output. A root search passes "." — rg and grep then
    # prefix every echoed path with "./", which is stripped afterwards
    # (safe: every output line starts with the path in that mode; explicit
    # single-file searches never pass "."). Omitting the path argument
    # instead would make rg read stdin, not search the cwd.
    if os.path.isabs(path):
        cwd = None
        search_arg = path
    else:
        cwd = str(file_service.get_project_path(project_id)) if project_id else "."
        search_arg = "." if path in ("", ".", "./") else path

    # Existence up front: a typo'd path must be an error, not a silent
    # "No matches". A directory search rooted at a virtual filesystem tree
    # (/proc, /sys, ...) is refused outright like glob — the timeout would
    # bound it, but a walk that can never usefully finish is an error, not a
    # slow search. Single files inside those trees stay allowed.
    probe = Path(search_arg) if cwd is None else Path(cwd) / search_arg
    if not probe.exists():
        return f"Error: Path does not exist: {path}"
    if probe.is_dir() and _is_virtual_fs_root(probe.resolve()):
        return _virtual_root_refusal(project_id, probe.resolve())

    timeout = _GREP_TIMEOUT_SECONDS
    cmd = _build_rg_command(
        pattern, search_arg,
        output_mode=output_mode, glob_filter=glob_filter, type_filter=type_filter,
        case_insensitive=case_insensitive, line_numbers=line_numbers,
        after_context=after_context, before_context=before_context,
        context=effective_context, multiline=multiline,
    )

    try:
        # Bounded search: on timeout the rg process is killed and partial
        # output is still surfaced. Exit-code classification separates
        # "no matches" from real failures (bad regex, resource errors) so
        # the LLM never reasons on a false "no matches".
        run = await _run_rg_search(cmd, cwd=cwd, timeout=timeout)
        stdout, bound_exceeded = run.stdout, run.bound_exceeded
        output = stdout.decode("utf-8", errors="replace").strip()
        if output and search_arg == ".":
            output = _strip_leading_dot_slash(output)
        output = _strip_trailing_cr(output)
        if bound_exceeded and output:
            # Killed mid-stream: the final line may be truncated mid-write.
            lines = output.split("\n")
            output = "\n".join(lines[:-1]) if len(lines) > 1 else ""
        if not output:
            if bound_exceeded:
                return f"grep error: search timed out after {timeout:g}s"
            if run.returncode not in (0, 1):
                raise SearchProcessError(run.returncode, run.stderr)
            return f"No matches for '{pattern}'"
        formatted = _format_grep_output(
            output,
            head_limit=head_limit, offset=offset,
        ) or f"No matches for '{pattern}'"
        if bound_exceeded:
            formatted += ("\n\n... (search stopped early — time or output "
                          "budget exceeded; use a more specific path or pattern)")
        return formatted
    except FileNotFoundError:
        # rg missing — fall back to grep with reduced feature set
        return await _grep_fallback(
            pattern, search_arg, cwd, glob_filter,
            output_mode=output_mode, case_insensitive=case_insensitive,
            context=effective_context, after_context=after_context,
            before_context=before_context, multiline=multiline,
            type_filter=type_filter,
            head_limit=head_limit, offset=offset,
        )
    except SearchProcessError as exc:
        return f"grep error: {exc}"
    except Exception as e:
        logger.exception("grep failed")
        return f"grep error: {e}"


def _strip_leading_dot_slash(output: str) -> str:
    """Remove the "./" prefix rg/grep put on every path for a "." search."""
    return "\n".join(
        line[2:] if line.startswith("./") else line
        for line in output.split("\n")
    )


def _strip_trailing_cr(output: str) -> str:
    """Drop the \\r rg echoes for CRLF files."""
    return "\n".join(
        line[:-1] if line.endswith("\r") else line
        for line in output.split("\n")
    )


async def _grep_fallback(
    pattern: str,
    search_arg: str,
    cwd: str | None,
    glob_filter: str,
    *,
    output_mode: str,
    case_insensitive: bool,
    context: int,
    after_context: int,
    before_context: int,
    multiline: bool,
    type_filter: str,
    head_limit: int,
    offset: int,
) -> str:
    """Best-effort grep fallback when ripgrep is unavailable.

    grep supports fewer flags than rg. We surface this to the LLM rather than
    silently dropping parameters: ``output_mode=count``, ``multiline``,
    ``-A/-B/-C/context``, and ``type`` have no grep equivalent and are
    reported as ignored.
    """
    ignored: list[str] = []
    if output_mode == "count":
        ignored.append("output_mode=count")
        effective_mode = "content"
    else:
        effective_mode = output_mode
    if multiline:
        ignored.append("multiline")
    if context or after_context or before_context:
        ignored.append("-A/-B/-C/context")
    if type_filter:
        ignored.append("type")

    # The root search passes "." (see the comment in _grep_search); the
    # "./" prefixes grep then echoes are stripped by the shared helper.
    root_search = search_arg == "."
    cmd = ["grep", "-rn"]
    if case_insensitive:
        cmd.append("-i")
    if glob_filter:
        cmd.append(f"--include={glob_filter}")
    # ``-e`` keeps patterns starting with ``-`` from being parsed as flags.
    cmd.extend(["-e", pattern, search_arg])

    timeout = _GREP_TIMEOUT_SECONDS
    try:
        run = await _run_bounded_search(cmd, cwd=cwd, timeout=timeout)
    except FileNotFoundError:
        return "grep error: neither ripgrep nor grep is available"
    output = run.stdout.decode("utf-8", errors="replace").strip()
    if output and root_search:
        output = _strip_leading_dot_slash(output)
    if output:
        output = _strip_trailing_cr(output)
    if run.bound_exceeded and output:
        # Killed mid-stream: the final line may be truncated mid-write.
        lines = output.split("\n")
        output = "\n".join(lines[:-1]) if len(lines) > 1 else ""
    if not output:
        if run.bound_exceeded:
            return f"grep error: search timed out after {timeout:g}s"
        # grep exits 2 on usage/regex errors — classify like the rg path so
        # a broken pattern is never reported as "No matches".
        if run.returncode == 2:
            first = run.stderr.decode(
                "utf-8", errors="replace").strip().splitlines()
            detail = first[0] if first else f"grep exited with code {run.returncode}"
            return f"grep error: {detail}"
        return f"No matches for '{pattern}'"

    if effective_mode == "files_with_matches":
        # grep -l would be cleaner, but we already passed -rn above; extract
        # unique file prefixes from "file:line:match" output.
        files = []
        seen_files: set[str] = set()
        for line in output.split("\n"):
            f = line.split(":", 1)[0]
            if f and f not in seen_files:
                seen_files.add(f)
                files.append(f)
        body = "\n".join(files)
    else:
        body = _format_grep_output(
            output,
            head_limit=head_limit, offset=offset,
        )

    suffix = ""
    if run.bound_exceeded:
        suffix = ("\n\n... (search stopped early — time or output "
                  "budget exceeded; use a more specific path or pattern)")
    if ignored:
        suffix += f"\n\n(rg not available; {', '.join(ignored)} ignored)"
    return body + suffix if body else f"No matches for '{pattern}'{suffix}"


# ── Permission-gate preflight for write/edit ────────────────────────
#
# These run in the permission executor before the approval dialog. They re-run
# the same must-read-first / invariant checks the tool body performs, so a call
# that is guaranteed to fail is rejected before bothering the user. Each returns
# an error string (fed back to the LLM) or None to proceed to the gate.

async def _write_preflight(
    project_id: str, session_id: str, file_path: str, **_extra,
) -> str | None:
    return _must_read_first_error_for(
        project_id, session_id, file_path, op="write",
    )


async def _edit_preflight(
    project_id: str, session_id: str, file_path: str,
    old_string: str = "", new_string: str = "", **_extra,
) -> str | None:
    if old_string == new_string:
        return "Error: old_string and new_string are identical. No changes needed."
    if file_path.lower().endswith(".ipynb"):
        return ("Error: file is a Jupyter notebook — use the notebook_edit "
                "tool to edit cells.")
    if not old_string:
        return ("Error: old_string must be non-empty. Use the write tool to "
                "create a file or replace its whole content.")
    return _must_read_first_error_for(
        project_id, session_id, file_path, op="edit",
    )


# ── Register file tools ──

tool_registry.register(ToolDefinition(
    name="read",
    prompt=PROMPT_READ,
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute or project-relative path"},
            "offset": {"type": "integer", "description": "0-indexed start line; must be >=0", "default": 0, "minimum": 0},
            "limit": {"type": "integer", "description": "Maximum lines to read. Omit or pass 0 for 200; negative returns last abs(limit) lines.", "default": 200},
        },
        "required": ["file_path"],
    },
    call=lambda file_path, project_id, session_id, model_role="", offset=None, limit=None: _read_file(
        project_id, session_id, file_path,
        offset if offset else None, limit,
        model_role=model_role,
    ),
    requires_project_id=True,
    requires_session_id=True,
    requires_model_role=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="write",
    prompt=PROMPT_WRITE,
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute or project-relative path"},
            "content": {"type": "string", "description": "Content to write to the file"},
        },
        "required": ["file_path", "content"],
    },
    call=lambda file_path, content, project_id, session_id: _write_file(project_id, session_id, file_path, content),
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=False,
    preflight=_write_preflight,
))

tool_registry.register(ToolDefinition(
    name="edit",
    prompt=PROMPT_EDIT,
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute or project-relative path"},
            "old_string": {"type": "string", "minLength": 1, "description": "The text to replace"},
            "new_string": {"type": "string", "description": "The text to replace it with (must be different from old_string)"},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)", "default": False},
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    call=lambda file_path, old_string, new_string, project_id, session_id, replace_all=False: _edit_file(
        project_id, session_id, file_path, old_string, new_string, replace_all,
    ),
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=False,
    preflight=_edit_preflight,
))

tool_registry.register(ToolDefinition(
    name="glob",
    prompt=PROMPT_GLOB,
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The glob pattern to match files against"},
            "path": {"type": "string", "description": "Absolute or project-relative directory", "default": "."},
        },
        "required": ["pattern"],
    },
    call=lambda pattern, path=".", project_id="": _glob_search(project_id, pattern, path),
    requires_project_id=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="grep",
    prompt=PROMPT_GREP,
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "The regex pattern to search for in file contents"},
            "path": {"type": "string", "description": "Absolute or project-relative file/directory", "default": "."},
            "glob": {"type": "string", "description": "Glob filter (e.g. '*.js', '*.{ts,tsx}'); passed to rg --glob", "default": ""},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "'content' shows matching lines; 'files_with_matches' (default) shows only file paths; 'count' shows match counts per file",
                "default": "files_with_matches",
            },
            "type": {"type": "string", "description": "Ripgrep --type filter (e.g. 'js', 'py', 'rust')", "default": ""},
            "-i": {"type": "boolean", "description": "Case-insensitive search", "default": False},
            "-n": {"type": "boolean", "description": "Show line numbers in content mode", "default": True},
            "-A": {"type": "integer", "description": "Lines of context after each match"},
            "-B": {"type": "integer", "description": "Lines of context before each match"},
            "-C": {"type": "integer", "description": "Lines of context around each match (overrides -A/-B)"},
            "context": {"type": "integer", "description": "Alias for -C; wins if both supplied"},
            "multiline": {"type": "boolean", "description": "Enable rg -U --multiline-dotall (cross-line matching)", "default": False},
            "head_limit": {"type": "integer", "description": "Cap on output lines; 0 or negative = unlimited", "default": _GREP_DEFAULT_HEAD_LIMIT},
            "offset": {"type": "integer", "description": "Skip N result entries; must be >=0", "default": 0, "minimum": 0},
        },
        "required": ["pattern"],
    },
    # ``**flags`` collects the hyphenated parameters (-i/-n/-A/-B/-C) which
    # cannot be expressed as Python parameter names.
    call=lambda pattern, path=".", glob="", output_mode="files_with_matches",
           type="", context=None, head_limit=_GREP_DEFAULT_HEAD_LIMIT, offset=0,
           multiline=False, project_id="", **flags: _grep_search(
        project_id, pattern, path, glob,
        output_mode=output_mode, type_filter=type,
        flags=flags, context=context,
        head_limit=head_limit, offset=offset, multiline=multiline,
    ),
    requires_project_id=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="ls",
    prompt=PROMPT_LS,
    input_schema={
        "type": "object",
        "properties": {
            "dirname": {"type": "string", "description": "Empty=project tree; relative=sandbox dir; absolute=host dir (read-only)", "default": ""},
        },
        "required": [],
    },
    call=lambda project_id, dirname="": _list_files(project_id, dirname),
    requires_project_id=True,
    is_read_only=True,
))
