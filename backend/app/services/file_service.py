import asyncio
import os
import hashlib
import difflib
import shutil
import stat
import zipfile
import tarfile
import io
import json
from pathlib import Path
from typing import List, Optional, Dict, Any
from enum import Enum
from app.core.exceptions import (
    FileSystemError, FileMissingError, FileAlreadyExistsError,
    InvalidPathError, ProjectNotFoundError, BinaryFileError,
)
from app.core.config import settings
from app.core.atomic_file import (
    ProjectFileLock, atomic_write_text, atomic_write_bytes, atomic_replace_bytes,
    AtomicFileExistsError,
)
from app.core.utils import is_within, sanitize_filename
from app.core.logging import get_logger
from app.services.snapshot_service import snapshot_service

logger = get_logger(__name__)

MAX_TREE_DEPTH = 64
MAX_TREE_NODES = 10000

# Whole-file read cap for agent tools. Agent-chosen paths can point at
# multi-GB datasets inside (or outside) the sandbox; reads stay unrestricted
# by design, but the *amount read in one call* is bounded. UI routes keep the
# historical uncapped behaviour by not passing ``max_bytes``. Larger files
# stay readable through the streaming range reader (``read_text_range``),
# which only accumulates the requested line window.
MAX_TOOL_READ_BYTES = 10 * 1024 * 1024

# Range-read budgets: a windowed read may scan the file to count lines, but
# the scan itself is bounded (a multi-TB file must not occupy a worker thread
# forever); the accumulated window bytes share the whole-read cap.
_RANGE_SCAN_MAX_BYTES = 1 << 30  # 1 GiB
_RANGE_CHUNK_BYTES = 1 << 20     # 1 MiB


def check_readable(p: Path, path_label: str, max_bytes: int | None) -> os.stat_result:
    """Validate a file before reading it on behalf of an agent tool.

    ``stat`` first, and only regular files pass: devices and fifos report
    size 0 yet never reach EOF (``/dev/zero``), so a byte cap alone cannot
    bound them. The size cap then bounds real files. Returns the stat result
    so callers avoid a second ``stat``. Error wording stays generic — the
    actionable "use offset/limit or bash sed" guidance is caller-specific and
    appended by the tool layer.
    """
    try:
        st = p.stat()
    except FileNotFoundError:
        raise FileMissingError(path_label)
    except OSError as e:
        raise FileSystemError(f"Cannot access file: {e}", code="INVALID_REQUEST")
    if not stat.S_ISREG(st.st_mode):
        raise FileSystemError(
            "Not a regular file (directories, devices, and special files "
            "are not readable)",
            code="INVALID_REQUEST",
        )
    if max_bytes is not None and st.st_size > max_bytes:
        raise FileSystemError(
            f"File is too large to read whole ({st.st_size:,} bytes exceeds the "
            f"{max_bytes:,}-byte cap)",
            code="FILE_TOO_LARGE",
        )
    return st


class TextFileContent:
    """Decoded text plus the encoding facts needed to write it back intact."""

    __slots__ = ("text", "encoding", "had_bom", "crlf")

    def __init__(self, text: str, encoding: str, had_bom: bool, crlf: bool):
        self.text = text
        self.encoding = encoding
        self.had_bom = had_bom
        self.crlf = crlf


def decode_text_bytes(raw: bytes) -> TextFileContent:
    """Decode file bytes BOM-aware, so UTF-16/UTF-8-sig text is readable.

    UTF-16 text is full of NUL bytes and would otherwise trip the binary
    check. Only BOM-marked UTF-16 is handled: BOM-less UTF-16 decodes as
    UTF-8 with replacement, same as before.
    """
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        endian = "le" if raw.startswith(b"\xff\xfe") else "be"
        text = raw.decode(f"utf-16-{endian}", errors="replace")
        # The BOM decodes to a stray \ufeff at the start of line 1.
        if text.startswith("\ufeff"):
            text = text[1:]
        encoding = f"utf-16-{endian}"
        had_bom = True
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8", errors="replace")
        encoding = "utf-8"
        had_bom = True
    else:
        text = raw.decode("utf-8", errors="replace")
        encoding = "utf-8"
        had_bom = False
    # Count on the decoded text: UTF-16's raw bytes never contain "\r\n" as
    # a contiguous pair, so a raw-byte heuristic misclassifies UTF-16 CRLF.
    crlf_count = text.count("\r\n")
    crlf = crlf_count > (text.count("\n") - crlf_count)
    return TextFileContent(text, encoding, had_bom, crlf)


class TextRange:
    """A windowed read of a text file: the selected lines plus their range.

    ``total_lines`` is ``None`` when the scan budget was hit before EOF — the
    window is valid but the caller cannot claim an exact total.
    """

    __slots__ = ("lines", "start_idx", "end_idx", "total_lines")

    def __init__(self, lines: list, start_idx: int, end_idx: int,
                 total_lines: Optional[int]):
        self.lines = lines
        self.start_idx = start_idx
        self.end_idx = end_idx
        self.total_lines = total_lines


def _read_text_range_sync(full_path: Path, offset: int, limit: int,
                          tail: Optional[int],
                          max_window_bytes: int) -> TextRange:
    """Streaming line-range read: only the requested window is accumulated.

    Files larger than ``MAX_TOOL_READ_BYTES`` stay readable through this
    path (whole-file reads would exceed the cap); lines outside the window
    are counted but discarded, so memory tracks the window, not the file.
    The scan stops at ``_RANGE_SCAN_MAX_BYTES`` even without reaching EOF —
    a hard bound on worker-thread occupancy.
    """
    with open(full_path, "rb") as f:
        header = f.read(8192)
        f.seek(0)
        header_info = decode_text_bytes(header)
        if header_info.encoding == "utf-8" and b"\x00" in header:
            raise BinaryFileError(str(full_path))
        strip_bom = header_info.had_bom
        # BOM-marked UTF-16: the newline is a 2-byte unit (\n\x00 / \x00\n);
        # splitting on the raw \n byte would cut between code units.
        utf16 = header_info.encoding != "utf-8"
        sep = (b"\n\x00" if header_info.encoding == "utf-16-le"
               else b"\x00\n") if utf16 else b"\n"
        sep_len = len(sep)

        def decode_line(line: bytes, idx: int) -> str:
            text = line.decode(
                header_info.encoding if utf16 else "utf-8",
                errors="replace")
            if idx == 0 and strip_bom and text.startswith("\ufeff"):
                text = text[1:]
            return text

        window: list = []
        window_bytes = 0
        tail_lines: list = []
        tail_lens: list = []
        line_idx = 0
        scanned = 0
        budget_hit = False
        eof = False
        buf = b""

        def take_line(payload: bytes, idx: int) -> None:
            nonlocal window_bytes
            line = decode_line(payload, idx)
            if tail is None:
                window.append(line)
            else:
                tail_lines.append(line)
                # Track payload byte lengths: the decoded string's length
                # differs from the byte count (UTF-16, multibyte UTF-8).
                tail_lens.append(len(payload) + sep_len)
            window_bytes += len(payload) + sep_len
            if tail is not None:
                while len(tail_lines) > tail:
                    window_bytes -= tail_lens.pop(0)
                    tail_lines.pop(0)
            if window_bytes > max_window_bytes:
                raise FileSystemError(
                    f"Requested line window exceeds the {max_window_bytes:,}-byte "
                    "cap — narrow the range (smaller limit), or use bash grep "
                    "to locate content first",
                    code="FILE_TOO_LARGE",
                )

        while not eof:
            chunk = f.read(_RANGE_CHUNK_BYTES)
            if not chunk:
                eof = True
            else:
                scanned += len(chunk)
            buf += chunk
            while True:
                nl = buf.find(sep)
                if nl < 0:
                    break
                payload = buf[:nl]
                buf = buf[nl + sep_len:]
                in_window = (
                    offset <= line_idx < offset + limit if tail is None
                    else True
                )
                if in_window:
                    take_line(payload, line_idx)
                line_idx += 1
            if not eof and scanned > _RANGE_SCAN_MAX_BYTES:
                budget_hit = True
                break
        if eof and buf:
            in_window = (
                offset <= line_idx < offset + limit if tail is None else True
            )
            if in_window:
                take_line(buf, line_idx)
            line_idx += 1

    if tail is not None:
        if budget_hit:
            # The collected lines are a prefix window, not the file tail.
            raise FileSystemError(
                f"File is too large to read its last lines (scan budget "
                f"exceeded at {_RANGE_SCAN_MAX_BYTES:,} bytes). Use an "
                "explicit offset window, or bash grep to locate content",
                code="FILE_TOO_LARGE",
            )
        start_idx = line_idx - len(tail_lines)
        return TextRange(list(tail_lines), start_idx, line_idx, line_idx)

    total = line_idx if not budget_hit else None
    if budget_hit and not window:
        # Never even reached the window start — nothing useful to return.
        raise FileSystemError(
            f"Requested lines start beyond the scan budget (first "
            f"{_RANGE_SCAN_MAX_BYTES:,} bytes). Use bash grep to locate the "
            "content, then read a narrower window",
            code="FILE_TOO_LARGE",
        )
    return TextRange(window, offset, offset + len(window), total)


def compute_diff_lines(old_text: str, new_text: str) -> list:
    """Compute a line-level diff between two texts using difflib.

    Returns ``{type, content}`` dicts where ``type`` is ``'context'``,
    ``'remove'``, or ``'add'``.
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    lines = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            for line in old_lines[i1:i2]:
                lines.append({'type': 'context', 'content': line.rstrip('\r\n')})
        elif op == 'replace':
            for line in old_lines[i1:i2]:
                lines.append({'type': 'remove', 'content': line.rstrip('\r\n')})
            for line in new_lines[j1:j2]:
                lines.append({'type': 'add', 'content': line.rstrip('\r\n')})
        elif op == 'delete':
            for line in old_lines[i1:i2]:
                lines.append({'type': 'remove', 'content': line.rstrip('\r\n')})
        elif op == 'insert':
            for line in new_lines[j1:j2]:
                lines.append({'type': 'add', 'content': line.rstrip('\r\n')})
    return lines


# ---------------------------------------------------------------------------
# Path access classification
# ---------------------------------------------------------------------------

class PathAccessLevel(str, Enum):
    """Permission level for a resolved filesystem path."""
    SANDBOX = "sandbox"       # Inside project directory → free R/W
    TMP = "tmp"               # Inside /tmp → free R/W
    EXTERNAL = "external"     # Outside sandbox & /tmp → read free, write needs approval


class FileService:
    def __init__(self):
        self.USERDATA_DIR = settings.USERDATA_DIR.resolve()

    # ------------------------------------------------------------------
    # Path access helpers
    # ------------------------------------------------------------------

    def _resolve_for_check(self, project_id: str, path: str | Path) -> Path:
        """Resolve a path for security checks.

        Relative paths are resolved against the project sandbox (matching the
        semantics of ``read_file``/``write_file`` via ``safe_join``); absolute
        paths are resolved following symlinks as before.
        """
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        # Relative path: treat as project-sandbox-relative. safe_join enforces
        # containment, so traversal attempts like ``../../etc/passwd`` raise
        # and are handled by the caller.
        try:
            sandbox = self.get_project_path(project_id)
            return self.safe_join(sandbox, str(p))
        except ProjectNotFoundError:
            # No sandbox context — fall back to CWD resolution so callers
            # still get a deterministic answer rather than crashing.
            return p.resolve()

    def classify_path(self, project_id: str, path: str) -> PathAccessLevel:
        """Classify a path into an access level."""
        try:
            resolved = self._resolve_for_check(project_id, path)
        except FileSystemError:
            # Relative path escaped the sandbox via traversal — treat as external.
            return PathAccessLevel.EXTERNAL

        try:
            sandbox = self.get_project_path(project_id)
            if is_within(resolved, sandbox):
                return PathAccessLevel.SANDBOX
        except ProjectNotFoundError:
            pass

        tmp_root = Path("/tmp").resolve()
        if is_within(resolved, tmp_root):
            return PathAccessLevel.TMP

        return PathAccessLevel.EXTERNAL

    def check_write_allowed(self, project_id: str, path: str) -> PathAccessLevel:
        return self.classify_path(project_id, path)

    async def read_file_absolute(self, path: str,
                                 max_bytes: int | None = None) -> str:
        """Read a file from an absolute host path (bypasses sandbox).

        ``max_bytes`` bounds agent-driven whole-file reads; the actual read
        runs off the event loop. BOM-marked UTF-16/UTF-8 files decode
        correctly via ``decode_text_bytes``.
        """
        p = Path(path).resolve()
        check_readable(p, path, max_bytes)
        raw = await asyncio.to_thread(p.read_bytes)
        info = decode_text_bytes(raw)
        if info.encoding == "utf-8" and b'\x00' in raw[:8192]:
            raise BinaryFileError(path)
        return info.text

    async def read_file_absolute_bytes(self, path: str,
                                       max_bytes: int | None = None) -> bytes:
        """Raw whole-file read from an absolute host path (BOM/binary checks
        are the caller's concern); bounded like ``read_file_absolute``."""
        p = Path(path).resolve()
        check_readable(p, path, max_bytes)
        return await asyncio.to_thread(p.read_bytes)

    async def read_text_range_absolute(
        self, path: str, *, offset: int = 0, limit: Optional[int] = None,
        tail: Optional[int] = None,
        max_window_bytes: int = MAX_TOOL_READ_BYTES,
    ) -> TextRange:
        """Windowed read of a text file on the host (see TextRange)."""
        p = Path(path).resolve()
        check_readable(p, path, None)
        return await asyncio.to_thread(
            _read_text_range_sync, p, offset, limit or 1, tail,
            max_window_bytes,
        )

    async def write_file_absolute(self, project_id: str, path: str,
                                  content: str, encoding: str = "utf-8") -> dict:
        """Write content to an absolute host path.

        The caller is responsible for ensuring the path passes the filesystem
        permission layer (``permission_executor._check_write``) before calling.

        If the resolved path lies inside the project sandbox, the write is
        treated like a project-file write for snapshot purposes — auto-snapshot
        is triggered so that absolute-path edits to project files are tracked
        exactly like their relative-path counterparts. Writes outside the
        sandbox (e.g. to ``/tmp``) do not trigger snapshot.
        """
        p = Path(path).resolve()
        atomic_write_text(p, content, encoding)

        # If the absolute path resolves inside the project sandbox, trigger
        # auto-snapshot so the edit is tracked.
        if self.classify_path(project_id, path) is PathAccessLevel.SANDBOX:
            await self._after_file_mutation(project_id)

        return {"conflict": False, "hash": self.compute_hash(content)}

    async def _after_file_mutation(self, project_id: str) -> None:
        """Post-mutation bookkeeping: refresh the project's modified time
        so the project list ordering tracks real activity, then trigger
        the auto-snapshot check."""
        from app.services.project_service import project_service
        project_service.touch_project(project_id)
        try:
            await snapshot_service.maybe_snapshot(project_id)
        except Exception:
            logger.debug("Auto-snapshot failed for project %s", project_id, exc_info=True)

    def safe_join(self, root: Path, *parts: str) -> Path:
        """Join path parts and verify it stays within root."""
        relative = "/".join(parts).lstrip("/")
        full_path = (root / relative).resolve()
        root_resolved = root.resolve()
        if not is_within(full_path, root_resolved):
            raise FileSystemError("Path traversal attempt detected", code="PERMISSION_DENIED", status_code=403)
        return full_path

    def get_project_path(self, project_id: str) -> Path:
        """Resolve and validate a project directory path."""
        from app.services.project_service import project_service
        return project_service.get_project_path(project_id)

    # ------------------------------------------------------------------
    # File tree
    # ------------------------------------------------------------------

    async def get_children(self, project_id: str, path: str = "") -> Dict[str, Any]:
        """Return immediate children of a directory (one level, non-recursive)."""
        root = self.get_project_path(project_id)
        target = self.safe_join(root, path) if path else root
        if not target.exists():
            raise FileMissingError(path)
        if not target.is_dir():
            raise FileSystemError("Not a directory", code="INVALID_REQUEST")

        def _scan() -> list:
            children = []
            for child in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if child.name.startswith('.'):
                    continue
                rel_path = str(child.relative_to(root))
                children.append({
                    "name": child.name,
                    "path": rel_path,
                    "type": "directory" if child.is_dir() else "file",
                })
            return children

        children = await asyncio.to_thread(_scan)
        return {"children": children, "parent": path}

    async def get_project_tree(self, project_id: str) -> Dict[str, Any]:
        root_path = self.get_project_path(project_id)
        visited: set[tuple[int, int]] = set()
        node_count = 0

        def build_tree(path: Path, depth: int = 0) -> Dict[str, Any]:
            nonlocal node_count
            node_count += 1
            name = path.name if path != root_path else "root"
            rel_path = str(path.relative_to(root_path)) if path != root_path else ""
            try:
                stat_result = path.stat(follow_symlinks=False)
            except OSError:
                return {"name": name, "path": rel_path, "type": "file", "children": [], "error": "unreadable"}

            is_symlink = stat.S_ISLNK(stat_result.st_mode)
            is_dir = stat.S_ISDIR(stat_result.st_mode)
            item = {
                "name": name,
                "path": rel_path,
                "type": "directory" if is_dir else "file",
                "children": [],
            }
            if is_symlink:
                item["symlink"] = True
                return item

            if not is_dir:
                return item

            inode_key = (stat_result.st_dev, stat_result.st_ino)
            if inode_key in visited:
                item["truncated"] = True
                return item
            visited.add(inode_key)

            if depth >= MAX_TREE_DEPTH or node_count >= MAX_TREE_NODES:
                item["truncated"] = True
                return item

            try:
                children = sorted(
                    list(path.iterdir()),
                    key=lambda x: (not self._is_plain_directory(x), x.name.lower()),
                )
            except OSError:
                item["error"] = "unreadable"
                return item

            for child in children:
                if child.name.startswith('.'):
                    continue
                if node_count >= MAX_TREE_NODES:
                    item["truncated"] = True
                    break
                item["children"].append(build_tree(child, depth + 1))
            return item

        # The recursive stat walk is blocking I/O — keep it off the event loop.
        root = await asyncio.to_thread(build_tree, root_path)
        return {"root": root}

    @staticmethod
    def _is_plain_directory(path: Path) -> bool:
        try:
            return stat.S_ISDIR(path.stat(follow_symlinks=False).st_mode)
        except OSError:
            return False

    @staticmethod
    def _validate_visible_relative_path(path: str) -> None:
        parts = Path(path).parts
        if not parts:
            raise InvalidPathError(path)
        for part in parts:
            if part in ("", ".", "..") or part.startswith("."):
                raise InvalidPathError(path)

    @staticmethod
    def _validate_visible_name(name: str) -> None:
        if "/" in name or "\\" in name or name in ("", ".", "..") or name.startswith("."):
            raise InvalidPathError(name)

    # ------------------------------------------------------------------
    # File read/write
    # ------------------------------------------------------------------

    async def read_file(self, project_id: str, path: str,
                        max_bytes: int | None = None) -> str:
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        check_readable(full_path, path, max_bytes)
        raw = await asyncio.to_thread(full_path.read_bytes)
        info = decode_text_bytes(raw)
        if info.encoding == "utf-8" and b'\x00' in raw[:8192]:
            raise BinaryFileError(path)
        return info.text

    async def read_text_range(
        self, project_id: str, path: str, *, offset: int = 0,
        limit: Optional[int] = None, tail: Optional[int] = None,
        max_window_bytes: int = MAX_TOOL_READ_BYTES,
    ) -> TextRange:
        """Windowed read of a sandbox text file (see TextRange)."""
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        check_readable(full_path, path, None)
        return await asyncio.to_thread(
            _read_text_range_sync, full_path, offset, limit or 1, tail,
            max_window_bytes,
        )

    async def read_file_binary(self, project_id: str, path: str,
                               max_bytes: int | None = None) -> bytes:
        """Read a file as raw bytes (for images and other binary formats)."""
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        check_readable(full_path, path, max_bytes)
        return await asyncio.to_thread(full_path.read_bytes)

    async def get_project_file_path(self, project_id: str, path: str) -> Path:
        """Resolve a project-relative file path for inline serving/download."""
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        if not full_path.exists():
            raise FileMissingError(path)
        if not full_path.is_file():
            raise FileSystemError("Not a file", code="INVALID_REQUEST")
        return full_path

    async def write_file(self, project_id: str, path: str, content: str,
                         force: bool = False, expected_hash: Optional[str] = None,
                         require_expected_hash: bool = False,
                         encoding: str = "utf-8") -> dict:
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)

        with ProjectFileLock(full_path):
            if not force and require_expected_hash and full_path.exists() and not expected_hash:
                try:
                    disk_content = full_path.read_text(encoding='utf-8', errors='replace')
                    diff_lines = compute_diff_lines(disk_content, content)
                    return {"conflict": True, "diff_lines": diff_lines}
                except OSError:
                    logger.warning("Conflict check failed for %s; refusing unguarded write", path, exc_info=True)
                    raise FileSystemError("Could not verify file version before saving", code="CONFLICT", status_code=409)

            if not force and expected_hash and full_path.exists():
                try:
                    disk_content = full_path.read_text(encoding='utf-8', errors='replace')
                    disk_hash = self.compute_hash(disk_content)
                    if disk_hash != expected_hash:
                        diff_lines = compute_diff_lines(disk_content, content)
                        return {"conflict": True, "diff_lines": diff_lines}
                except OSError:
                    logger.warning("Conflict check failed for %s; refusing guarded write", path, exc_info=True)
                    raise FileSystemError("Could not verify file version before saving", code="CONFLICT", status_code=409)

            full_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_replace_bytes(full_path, content.encode(encoding))

        await self._after_file_mutation(project_id)
        return {"conflict": False, "hash": self.compute_hash(content)}

    @staticmethod
    def compute_hash(text: str) -> str:
        """Compute MD5 hex digest of text content (UTF-8 encoded)."""
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    # ------------------------------------------------------------------
    # File create / delete / move / rename
    # ------------------------------------------------------------------

    async def create_item(self, project_id: str, path: str, is_dir: bool):
        self._validate_visible_relative_path(path)
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        if full_path.exists(): raise FileAlreadyExistsError(path)
        if is_dir: full_path.mkdir(parents=True, exist_ok=True)
        else:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            if path.lower().endswith('.ipynb'):
                empty_nb = {
                    "cells": [
                        {
                            "cell_type": "code",
                            "execution_count": None,
                            "id": "default-cell",
                            "metadata": {},
                            "outputs": [],
                            "source": [],
                        }
                    ],
                    "metadata": {
                        "kernelspec": {
                            "display_name": "Python 3",
                            "language": "python",
                            "name": "python3",
                        },
                        "language_info": {
                            "name": "python",
                            "version": "3.11.0",
                        },
                    },
                    "nbformat": 4,
                    "nbformat_minor": 5,
                }
                atomic_write_text(full_path, json.dumps(empty_nb, indent=1))
            else:
                full_path.touch()
        await self._after_file_mutation(project_id)

    async def delete_item(self, project_id: str, path: str):
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        if not full_path.exists(): raise FileMissingError(path)
        if full_path.is_dir(): shutil.rmtree(full_path)
        else: full_path.unlink()
        await self._after_file_mutation(project_id)

    async def move_item(self, project_id: str, src_path: str, dest_path: str):
        root = self.get_project_path(project_id)
        src = self.safe_join(root, src_path)
        dest = self.safe_join(root, dest_path if dest_path else ".")
        if not src.exists(): raise FileMissingError(src_path)
        if dest.exists() and dest.is_file() and src != dest:
            raise FileSystemError("Target must be a directory", code="INVALID_REQUEST")
        final_dest = dest / src.name if dest.is_dir() else dest
        if final_dest.exists() and final_dest != src:
            raise FileAlreadyExistsError(src.name)
        if is_within(final_dest, src) and final_dest != src: raise FileSystemError("Cannot move into self", code="INVALID_REQUEST")
        shutil.move(str(src), str(final_dest))
        await self._after_file_mutation(project_id)

    async def rename_item(self, project_id: str, old_path: str, new_name: str):
        self._validate_visible_name(new_name)
        root = self.get_project_path(project_id)
        old = self.safe_join(root, old_path)
        new = old.parent / new_name
        if not old.exists(): raise FileMissingError(old_path)
        if new.exists(): raise FileAlreadyExistsError(new_name)
        old.rename(new)
        await self._after_file_mutation(project_id)

    # ------------------------------------------------------------------
    # Upload / download
    # ------------------------------------------------------------------

    async def save_upload(self, project_id: str, filename: str, content: bytes, path: str = "", overwrite: bool = False) -> str:
        """Save an uploaded file. Returns the saved filename."""
        safe_name = sanitize_filename(filename)
        root = self.get_project_path(project_id)
        dest_dir = self.safe_join(root, path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / safe_name
        try:
            atomic_write_bytes(dest_path, content, fail_if_exists=not overwrite)
        except AtomicFileExistsError:
            raise FileAlreadyExistsError(safe_name)
        await self._after_file_mutation(project_id)
        return safe_name

    async def get_download_info(self, project_id: str, path: str) -> dict:
        """Get download info for a file/directory path."""
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        if not full_path.exists():
            raise FileMissingError(path)
        return {"full_path": full_path, "is_file": full_path.is_file(), "name": full_path.name}

    # ------------------------------------------------------------------
    # Archives
    # ------------------------------------------------------------------

    async def create_zip(self, project_id: str, path: str = "") -> bytes:
        root = self.get_project_path(project_id)
        target = self.safe_join(root, path)
        if not target.exists(): raise FileMissingError(path)
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if target.is_dir():
                for root_dir, _, files in os.walk(target):
                    for file in files:
                        file_path = Path(root_dir) / file
                        zf.write(file_path, file_path.relative_to(target))
            else: zf.write(target, target.name)
        return memory_file.getvalue()

    async def create_multi_zip(self, project_id: str, paths: List[str]) -> bytes:
        """Create an in-memory ZIP containing multiple files/directories."""
        root = self.get_project_path(project_id)
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                target = self.safe_join(root, p)
                if not target.exists():
                    continue
                if target.is_file():
                    zf.write(target, target.name)
                else:
                    for dir_root, _, files in os.walk(target):
                        for f in files:
                            file_path = Path(dir_root) / f
                            arcname = str(file_path.relative_to(target.parent))
                            zf.write(file_path, arcname)
        return memory_file.getvalue()

    # ------------------------------------------------------------------
    # Archive extraction
    # ------------------------------------------------------------------

    @staticmethod
    def sanitize_member(name: str) -> str:
        """Validate an archive member name for safe extraction.

        Returns the member name unchanged if it is a safe relative path,
        or an empty string if the member should be skipped entirely.

        Skips members that contain absolute paths, parent-traversal (``..``),
        Windows drive letters, home-directory references (``~``), or embedded
        separators — any of which indicates a suspicious or malicious entry
        that should not be extracted under a rewritten name.
        """
        # Reject absolute paths (POSIX and Windows)
        if name.startswith('/') or name.startswith('\\'):
            return ""
        if len(name) >= 2 and name[1] == ':':
            return ""
        # Reject home-directory references
        if name.startswith('~'):
            return ""
        parts = Path(name).parts
        for p in parts:
            # Reject parent traversal
            if p == '..':
                return ""
            # Reject root separators and empty parts
            if p in ('/', '\\', ''):
                return ""
            # Reject Windows drive letters
            if len(p) == 2 and p.endswith(':'):
                return ""
            # Reject components containing embedded separators
            if '/' in p or '\\' in p:
                return ""
        return name

    def _validate_extract_dest(
        self, dest: Path, target_dir: Path, project_root: Path,
    ) -> bool:
        """Verify *dest* resolves to within both *target_dir* and *project_root*.

        The double containment check prevents symlink-based escapes: if
        *target_dir* is a symlink pointing outside the project, the
        ``project_root`` check will reject the destination.
        """
        try:
            resolved = dest.resolve()
            return (
                is_within(resolved, target_dir.resolve())
                and is_within(resolved, project_root.resolve())
            )
        except (OSError, ValueError):
            return False

    @staticmethod
    def _archive_target_dirname(filename: str) -> str:
        """Derive the extraction folder name from an archive filename."""
        if filename.endswith('.tar.gz') or filename.endswith('.tgz'):
            return filename.rsplit('.', 2)[0]
        if filename.endswith('.tar'):
            return filename.rsplit('.', 1)[0]
        return Path(filename).stem

    def _list_archive_members(self, full_path: Path) -> list[str]:
        """Return sanitized member names from a zip/tar archive."""
        members = []
        name = full_path.name
        if name.endswith('.zip'):
            with zipfile.ZipFile(full_path, 'r') as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    clean = self.sanitize_member(info.filename)
                    if clean:
                        members.append(clean)
        elif name.endswith('.tar.gz') or name.endswith('.tgz'):
            with tarfile.open(full_path, 'r:gz') as tf:
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    clean = self.sanitize_member(m.name)
                    if clean:
                        members.append(clean)
        elif name.endswith('.tar'):
            with tarfile.open(full_path, 'r:') as tf:
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    clean = self.sanitize_member(m.name)
                    if clean:
                        members.append(clean)
        return members

    async def check_extract_conflicts(self, project_id: str, path: str) -> list[str]:
        """Return list of relative paths that would be overwritten by extraction."""
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        if not full_path.exists():
            raise FileMissingError(path)

        target_dir = full_path.parent / self._archive_target_dirname(full_path.name)
        conflicts = []

        if target_dir.is_symlink():
            return [str(target_dir.relative_to(root))]
        if target_dir.exists() and not target_dir.is_dir():
            conflicts.append(str(target_dir.relative_to(root)))

        members = self._list_archive_members(full_path)
        for member in members:
            try:
                dest = target_dir / member
                if not self._validate_extract_dest(dest, target_dir, root):
                    continue
                resolved = dest.resolve()
                if is_within(resolved, root) and resolved.exists():
                    conflicts.append(str(resolved.relative_to(root)))
            except Exception:
                logger.debug("Failed to inspect archive member %s", member, exc_info=True)
        return conflicts

    def _ensure_parent_dirs(self, dest: Path, overwrite: bool = False) -> bool:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            return True
        except (FileExistsError, NotADirectoryError):
            if not overwrite:
                return False
            p = dest.parent
            while p != p.parent:
                if p.exists() and not p.is_dir():
                    p.unlink()
                elif not p.exists():
                    break
                p = p.parent
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                return True
            except OSError:
                return False

    def _extract_member(self, src_file, dest: Path, overwrite: bool) -> bool:
        if not self._ensure_parent_dirs(dest, overwrite):
            return False
        try:
            data = src_file.read()
            atomic_write_bytes(dest, data, fail_if_exists=not overwrite)
            return True
        except AtomicFileExistsError:
            return False
        except (OSError, NotADirectoryError):
            return False

    async def extract_archive(self, project_id: str, path: str, overwrite: bool = False) -> dict:
        """Extract a zip/tar archive into a folder named after the archive."""
        root = self.get_project_path(project_id)
        full_path = self.safe_join(root, path)
        if not full_path.exists():
            raise FileMissingError(path)

        target_dirname = self._archive_target_dirname(full_path.name)
        target_dir = full_path.parent / target_dirname
        # Reject symlinks — never follow a symlink when creating target_dir
        if target_dir.is_symlink():
            raise FileSystemError(
                f"Extraction target is a symlink: {target_dirname}",
                code="INVALID_REQUEST",
            )
        if target_dir.exists() and not target_dir.is_dir():
            if overwrite:
                target_dir.unlink()
            else:
                raise FileAlreadyExistsError(target_dirname)
        target_dir.mkdir(parents=True, exist_ok=True)

        name = full_path.name
        count = 0

        if name.endswith('.zip'):
            with zipfile.ZipFile(full_path, 'r') as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    clean = self.sanitize_member(info.filename)
                    if not clean:
                        continue
                    dest = target_dir / clean
                    if not self._validate_extract_dest(dest, target_dir, root):
                        continue
                    with zf.open(info) as src:
                        if self._extract_member(src, dest, overwrite):
                            count += 1
        elif name.endswith('.tar.gz') or name.endswith('.tgz'):
            with tarfile.open(full_path, 'r:gz') as tf:
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    clean = self.sanitize_member(m.name)
                    if not clean:
                        continue
                    dest = target_dir / clean
                    if not self._validate_extract_dest(dest, target_dir, root):
                        continue
                    src = tf.extractfile(m)
                    if src and self._extract_member(src, dest, overwrite):
                        count += 1
        elif name.endswith('.tar'):
            with tarfile.open(full_path, 'r:') as tf:
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    clean = self.sanitize_member(m.name)
                    if not clean:
                        continue
                    dest = target_dir / clean
                    if not self._validate_extract_dest(dest, target_dir, root):
                        continue
                    src = tf.extractfile(m)
                    if src and self._extract_member(src, dest, overwrite):
                        count += 1
        else:
            raise FileSystemError(f"Unsupported archive format: {name}", code="INVALID_REQUEST")

        await self._after_file_mutation(project_id)
        return {"extracted_to": target_dirname, "file_count": count}


file_service = FileService()
