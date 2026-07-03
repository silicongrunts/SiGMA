"""
File operation tools — read, write, edit, glob, grep, list_files.
"""

import asyncio
import base64
import os
import re
from pathlib import Path

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.tools.read_state import (
    path_mtime,
    path_read_state_key,
    read_state_cache,
    record_path_read,
    verify_path_readable_fresh,
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
from app.core.chat_attachments import render_image_refs_tag
from app.services.file_service import file_service

logger = get_logger(__name__)

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
) -> None:
    """Record a read in the per-session cache (must-read-first enforcement)."""
    try:
        record_path_read(
            session_id, _resolve_file_path(project_id, file_path), content, is_partial,
        )
    except (OSError, ProjectNotFoundError, FileSystemError):
        read_state_cache.record_read(
            session_id, file_path, content, 0.0, is_partial,
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

    # Absolute host path: read directly. Binary errors are surfaced as-is —
    # falling through to sandbox resolution produced misleading "not found"
    # errors when the binary file actually existed on the host.
    if filename.startswith('/'):
        try:
            content = file_service.read_file_absolute(filename)
        except FileMissingError:
            return f"Error: File not found: {filename}"
        except BinaryFileError:
            return f"Error: File is binary: {filename}"

    # Relative path: resolve via project sandbox.
    if content is None:
        try:
            content = await file_service.read_file(project_id, filename)
        except FileMissingError:
            return f"Error: File not found: {filename}"
        except BinaryFileError:
            return f"Error: File is binary: {filename}"
        # Empty string is valid — the file exists but is empty

    return _slice_and_record(
        project_id, session_id, filename, content, offset, limit,
    )


def _slice_and_record(
    project_id: str, session_id: str, file_path: str,
    content: str, offset: int | None, limit: int | None,
) -> str:
    """Apply offset/limit slicing and record the read in the cache.

    ``offset`` is 0-indexed: ``offset=0`` (or ``None``) starts at line 1.
    ``limit=None`` or ``limit=0`` triggers the default 200-line cap with a
    "more lines not shown" suffix when applicable. ``limit<0`` returns the
    last ``abs(limit)`` lines and ignores ``offset``.
    """
    if (limit is None or limit >= 0) and offset is not None and offset < 0:
        return f"Error: offset must be >= 0, got {offset}"

    is_partial = offset is not None or limit is not None
    _record_read(project_id, session_id, file_path, content, is_partial=is_partial)

    lines = content.split("\n")
    if limit is not None and limit < 0:
        start_idx = max(0, len(lines) + limit)
        end_idx = len(lines)
        defaulted = False
    else:
        start_idx = max(0, offset) if offset else 0
        if limit is None or limit == 0:
            end_idx = min(len(lines), start_idx + _DEFAULT_READ_LIMIT)
            defaulted = True
        else:
            end_idx = min(len(lines), start_idx + limit)
            defaulted = False

    result = "\n".join(lines[start_idx:end_idx])
    if defaulted and end_idx < len(lines):
        result += f"\n\n... ({len(lines) - end_idx} more lines not shown)"
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
    # Read raw bytes — absolute or sandbox path
    try:
        if filename.startswith('/'):
            p = Path(filename).resolve()
            if not p.exists():
                return f"Error: File not found: {filename}"
            if not p.is_file():
                return f"Error: Not a file: {filename}"
            raw = p.read_bytes()
        else:
            raw = await file_service.read_file_binary(project_id, filename)
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
    if w > _MAX_IMAGE_DIMENSION or h > _MAX_IMAGE_DIMENSION:
        return (
            f"Error: Image resolution {w}\u00d7{h} exceeds the "
            f"{_MAX_IMAGE_DIMENSION}\u00d7{_MAX_IMAGE_DIMENSION} limit. "
            "Please resize the image."
        )

    # Images are read as one unit — no offset/limit applies.
    _record_read(project_id, session_id, filename, content="", is_partial=False)

    return {
        "type": "image",
        "image_base64": base64.b64encode(raw).decode("ascii"),
        "media_type": media_type,
        "text": f"Image file: {filename} ({w}\u00d7{h})",
        "image_ref": {
            "path": filename,
            "mime_type": media_type,
            "name": Path(filename).name,
            "source": "read",
            "text": f"Image file: {filename} ({w}\u00d7{h})",
        },
    }


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
    if not _verify_readable_fresh(project_id, session_id, file_path):
        return (
            f"Error: File '{file_path}' has not been read yet (or has changed "
            "since the last read). Use the read tool first, then retry the "
            "write."
        )

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
    conversation segment.
    """
    if old_string == new_string:
        return "Error: old_string and new_string are identical. No changes needed."

    if not _verify_readable_fresh(project_id, session_id, file_path):
        return (
            f"Error: File '{file_path}' has not been read yet (or has changed "
            "since the last read). Use the read tool first, then retry the "
            "edit."
        )

    # Read file content — supports absolute and relative paths.
    content = None
    if file_path.startswith('/'):
        try:
            content = file_service.read_file_absolute(file_path)
        except (FileMissingError, BinaryFileError):
            pass

    if content is None:
        try:
            content = await file_service.read_file(project_id, file_path)
        except FileMissingError:
            return f"Error: File not found: {file_path}"
        except BinaryFileError:
            return f"Error: File is binary: {file_path}"

    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {file_path}"
    if count > 1 and not replace_all:
        return f"Error: old_string appears {count} times in {file_path}. Use replace_all=true or provide more context."

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)

    # Write back — permission already checked by permission_executor._check_write
    if file_path.startswith('/'):
        await file_service.write_file_absolute(project_id, file_path, new_content)
    else:
        await file_service.write_file(project_id, file_path, new_content)

    # Refresh cache so further edits in the same turn pass the staleness check.
    _record_read(project_id, session_id, file_path, new_content, is_partial=False)
    return f"File edited: {file_path} ({count} replacement(s))"


def _verify_readable_fresh(
    project_id: str, session_id: str, file_path: str,
) -> bool:
    """Return True iff *file_path* may be modified under the must-read-first rule.

    The check is two-pronged:
    1. The file must have been read earlier in this conversation segment.
       Paginated reads with offset/limit satisfy this requirement.
    2. The file's current mtime must match the mtime captured at read time —
       if the file changed on disk (e.g. the user edited it in another tab),
       the cached read is stale and the LLM must re-read.

    Non-existing files are exempt (creating a new file does not require a
    prior read).
    """
    # New files bypass the check
    try:
        exists = _resolve_file_path(project_id, file_path).is_file()
    except (OSError, ProjectNotFoundError, FileSystemError):
        exists = False
    if not exists:
        return True

    entry = read_state_cache.get(session_id, _read_state_key(project_id, file_path))
    if entry is None:
        return False

    try:
        return verify_path_readable_fresh(
            session_id, _resolve_file_path(project_id, file_path),
        )
    except (OSError, ProjectNotFoundError, FileSystemError):
        return False


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
            entries = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            lines = []
            for e in entries:
                if e.name.startswith('.'):
                    continue
                suffix = "/" if e.is_dir() else ""
                lines.append(f"{e.name}{suffix}")
            return "\n".join(lines) if lines else "(empty directory)"

        # Relative path or empty — browse project sandbox
        if dirname:
            result = await file_service.get_children(project_id, dirname)
            children = result.get("children", [])
            if not children:
                return "(empty directory)"
            lines = []
            for c in children:
                suffix = "/" if c["type"] == "directory" else ""
                lines.append(f"{c['name']}{suffix}")
            return "\n".join(lines)

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
        return "\n".join(lines) if lines else "(empty project)"
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


async def _glob_search(project_id: str = "", pattern: str = "", path: str = ".") -> str:
    """Find files matching a glob pattern.

    Results are sorted by modification time (newest first), with alphabetical
    order as a tiebreaker. When ``path`` is a relative subdirectory, returned
    paths are still relative to the project root (so the LLM can pass them
    directly to ``read``); when ``path`` is absolute, returned paths are
    absolute.
    """
    import glob as glob_mod

    search_dir = path if os.path.isabs(path) else os.path.join(
        str(file_service.get_project_path(project_id)) if project_id else ".", path
    )

    path_is_absolute = os.path.isabs(path)
    # For relative subdirectory ``path`` (e.g. "src"), glob returns paths
    # relative to search_dir (e.g. "foo.ts"); prepend the subdirectory so the
    # LLM sees project-relative paths ("src/foo.ts").
    rel_prefix = "" if (path_is_absolute or path in (".", "", "./")) else path.rstrip("/") + "/"

    # Expand brace patterns and collect deduplicated results
    seen: set[str] = set()
    matches: list[str] = []
    for sub_pattern in _expand_braces(pattern):
        for m in glob_mod.glob(sub_pattern, root_dir=search_dir, recursive=True):
            full = m if path_is_absolute else rel_prefix + m
            if full not in seen:
                seen.add(full)
                matches.append(full)

    if not matches:
        return f"No files matching '{pattern}'"

    # Sort by mtime desc, then alphabetically. Best-effort: stat failures fall
    # back to mtime=0 (oldest).
    def _mtime_key(p: str) -> tuple[float, str]:
        try:
            if path_is_absolute:
                return (-Path(p).stat().st_mtime, p)
            base = file_service.get_project_path(project_id) if project_id else Path(".")
            return (-(base / p).stat().st_mtime, p)
        except OSError:
            return (0.0, p)
    matches.sort(key=_mtime_key)

    shown = matches[:_GLOB_MAX_RESULTS]
    result = "\n".join(shown)
    if len(matches) > _GLOB_MAX_RESULTS:
        result += f"\n... ({len(matches) - _GLOB_MAX_RESULTS} more matches not shown)"
    return result


# ── grep (ripgrep-backed content search) ────────────────────────────

_GREP_DEFAULT_HEAD_LIMIT = 250
_GREP_TIMEOUT_SECONDS = 15


def _build_rg_command(
    pattern: str,
    full_path: str,
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
    cmd.extend(["-e", pattern, full_path])
    return cmd


def _format_grep_output(
    raw: str,
    *,
    output_mode: str,
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

    # files_with_matches mode: also sort by mtime (newest first), then alpha
    if output_mode == "files_with_matches":
        try:
            base = Path(".")  # placeholder; sort key computed by caller
        except Exception:
            pass

    if head_limit and head_limit > 0:
        end = min(total, offset + head_limit)
    else:
        end = total
    shown = all_lines[offset:end]

    result = "\n".join(shown)
    if head_limit and head_limit > 0 and end < total:
        result += (
            f"\n\n[Showing results with pagination = "
            f"limit: {head_limit}, offset: {offset}]"
        )
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

    if os.path.isabs(path):
        full_path = path
    else:
        base = str(file_service.get_project_path(project_id)) if project_id else "."
        full_path = os.path.join(base, path)

    cmd = _build_rg_command(
        pattern, full_path,
        output_mode=output_mode, glob_filter=glob_filter, type_filter=type_filter,
        case_insensitive=case_insensitive, line_numbers=line_numbers,
        after_context=after_context, before_context=before_context,
        context=effective_context, multiline=multiline,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_GREP_TIMEOUT_SECONDS)
        output = stdout.decode("utf-8", errors="replace").strip()
        if not output:
            return f"No matches for '{pattern}'"
        return _format_grep_output(
            output, output_mode=output_mode,
            head_limit=head_limit, offset=offset,
        ) or f"No matches for '{pattern}'"
    except FileNotFoundError:
        # rg missing — fall back to grep with reduced feature set
        return await _grep_fallback(
            pattern, full_path, glob_filter,
            output_mode=output_mode, case_insensitive=case_insensitive,
            context=effective_context, after_context=after_context,
            before_context=before_context, multiline=multiline,
            type_filter=type_filter,
            head_limit=head_limit, offset=offset,
        )
    except asyncio.TimeoutError:
        return f"grep error: search timed out after {_GREP_TIMEOUT_SECONDS}s"
    except Exception as e:
        logger.exception("grep failed")
        return f"grep error: {e}"


async def _grep_fallback(
    pattern: str,
    full_path: str,
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

    cmd = ["grep", "-rn"]
    if case_insensitive:
        cmd.append("-i")
    if glob_filter:
        cmd.append(f"--include={glob_filter}")
    cmd.extend([pattern, full_path])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_GREP_TIMEOUT_SECONDS)
        output = stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        return f"grep error: search timed out after {_GREP_TIMEOUT_SECONDS}s"
    except Exception as e:
        logger.exception("grep subprocess failed")
        return f"grep error: {e}"

    if not output:
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
            output, output_mode="content",
            head_limit=head_limit, offset=offset,
        )

    suffix = ""
    if ignored:
        suffix = f"\n\n(rg not available; {', '.join(ignored)} ignored)"
    return body + suffix if body else f"No matches for '{pattern}'{suffix}"


# ── Register file tools ──

tool_registry.register(ToolDefinition(
    name="read",
    description="Read a text/PDF/image file from the local filesystem.",
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
    description="Write content to a file. Overwrites if exists. Prefer Edit for modifications.",
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
))

tool_registry.register(ToolDefinition(
    name="edit",
    description="Perform exact string replacements in an existing file.",
    prompt=PROMPT_EDIT,
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute or project-relative path"},
            "old_string": {"type": "string", "description": "The text to replace"},
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
))

tool_registry.register(ToolDefinition(
    name="glob",
    description="Fast file pattern matching tool. Find files by glob patterns.",
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
    description="Search file contents using regex. Built on ripgrep.",
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
    description="List files and directories in the project (or any host directory).",
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
