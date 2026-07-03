"""
Skill Service — discovers, caches and serves skills from the global skill directory.

Skills live as folders under ``userdata/.SiGMA/skill/``.  Each folder represents
one skill and must contain a ``SKILL.md`` file with YAML frontmatter (``---``
delimiters).  A folder whose name starts with a dot (e.g. ``.my-skill``) is
considered *disabled*.

Only ``name`` and ``description`` fields from the frontmatter are used; all
other fields are silently ignored for forward-compatibility with external
skill file formats.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from datetime import datetime, timezone

import yaml

from app.core.config import settings
from app.core.exceptions import SkillError, SkillNotFoundError
from app.core.logging import get_logger
from app.core.atomic_file import atomic_write_text

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_DESC_LEN = 200          # truncate description in prompt listing
_MAX_PROMPT_CHARS = 2000     # hard cap for <skills> block
_MAX_FILE_BYTES = 1_000_000  # 1 MB
_BINARY_CHECK_BYTES = 8192   # first 8 KB checked for null bytes
_SKILL_MD = "SKILL.md"

# Import limits
_MAX_ZIP_SIZE = 50_000_000    # 50 MB uploaded ZIP
_MAX_EXTRACT_TOTAL = 100_000_000  # 100 MB total extracted
_MAX_EXTRACT_PER_FILE = 10_000_000  # 10 MB per extracted file
_MAX_EXTRACT_ENTRIES = 500    # max files in a ZIP
_CLONE_TIMEOUT = 60          # seconds

_FRONTMATTER_RE = re.compile(r"^---\s*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse YAML frontmatter from a SKILL.md string.

    Returns a dict with at most ``name`` and ``description``.  Any parse
    error is swallowed — the caller receives a (possibly empty) dict.
    """
    parts = _FRONTMATTER_RE.split(text, maxsplit=2)
    if len(parts) >= 3:
        yaml_block = parts[1]
    else:
        # No frontmatter found — return empty
        return {}
    try:
        data = yaml.safe_load(yaml_block)
        if not isinstance(data, dict):
            return {}
    except yaml.YAMLError:
        return {}
    out: dict[str, Any] = {}
    if "name" in data:
        out["name"] = str(data["name"])
    if "description" in data:
        out["description"] = str(data["description"])
    return out


def _is_binary(file_path: Path) -> bool:
    """Heuristic: a file is binary if the first 8 KB contain a null byte."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(_BINARY_CHECK_BYTES)
        return b"\x00" in chunk
    except OSError:
        return True


def _mtime_to_iso(mtime: float) -> str:
    """Convert a Unix timestamp to ISO 8601 string."""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _slugify(name: str) -> str:
    """Convert a skill name to a safe directory name.

    Lowercases, replaces non-alphanumeric runs with hyphens,
    strips leading/trailing hyphens, and caps at 64 chars.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:64] or "skill"


def _short_id() -> str:
    """Generate an 8-char random hex string for collision resolution."""
    return uuid.uuid4().hex[:8]


# ---------------------------------------------------------------------------
# SkillService
# ---------------------------------------------------------------------------

class SkillService:
    """Singleton-like service that manages the global skill store."""

    def __init__(self) -> None:
        self._skills_dir: Path = settings.SIGMA_DIR / "skill"
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        # Cache: id -> {name, description, enabled, mtime}
        self._cache: dict[str, dict] = {}
        self._dir_mtime: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_all_skills(self) -> list[dict]:
        """Return all skills sorted: enabled first, then disabled, alphabetically by name."""
        self._maybe_refresh()
        skills = [
            {
                "id": sid,
                "name": info["name"],
                "description": info["description"],
                "enabled": info["enabled"],
            }
            for sid, info in self._cache.items()
        ]
        skills.sort(key=lambda s: (0 if s["enabled"] else 1, s["name"].lower()))
        return skills

    def get_skill_content(self, skill_id: str, file_path: str | None = None) -> str:
        """Read and return the content of a file inside a skill directory.

        This is the LLM-facing API (used by ``skill_load`` tool).

        Args:
            skill_id: Folder name of the skill (may include leading dot).
            file_path: Relative path inside the skill directory.  Defaults to
                       ``SKILL.md``.

        Returns:
            File content as UTF-8 string.

        Raises:
            SkillNotFoundError: skill directory not found.
            SkillError: path traversal, binary file, file too large, etc.
        """
        _id, skill_dir = self._resolve_skill(skill_id)

        target_name = file_path or _SKILL_MD
        if not target_name or target_name.startswith("/"):
            raise SkillError("file_path must be a relative path without leading slash")

        target = self._safe_join(skill_dir, target_name)

        if not target.exists() or not target.is_file():
            raise SkillError(f"File not found in skill: {target_name}")

        if _is_binary(target):
            raise SkillError(f"Cannot read binary file: {target_name}")

        size = target.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise SkillError(f"File too large ({size} bytes, max {_MAX_FILE_BYTES}): {target_name}")

        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SkillError(f"Failed to read {target_name}: {exc}") from exc

    def toggle_skill(self, skill_id: str) -> dict:
        """Toggle a skill between enabled and disabled by renaming its folder.

        Returns the updated skill info dict.
        """
        self._maybe_refresh()

        if skill_id not in self._cache:
            raise SkillNotFoundError(skill_id)

        old_path = self._skills_dir / skill_id
        if skill_id.startswith("."):
            new_name = skill_id[1:]
        else:
            new_name = f".{skill_id}"

        new_path = self._skills_dir / new_name
        try:
            os.rename(old_path, new_path)
        except OSError as exc:
            raise SkillError(f"Failed to toggle skill: {exc}") from exc

        # Invalidate cache
        self._invalidate()
        self._maybe_refresh()

        # Return updated skill (now under new name)
        if new_name in self._cache:
            info = self._cache[new_name]
            return {
                "id": new_name,
                "name": info["name"],
                "description": info["description"],
                "enabled": info["enabled"],
            }
        # Fallback — should not happen
        raise SkillError("Skill state inconsistent after toggle")

    def delete_skill(self, skill_id: str) -> dict:
        """Delete a skill directory entirely.

        Returns the deleted skill info dict.
        """
        self._maybe_refresh()

        if skill_id not in self._cache:
            raise SkillNotFoundError(skill_id)

        info = self._cache[skill_id]
        skill_dir = self._safe_resolve(skill_id)

        try:
            shutil.rmtree(skill_dir)
        except OSError as exc:
            raise SkillError(f"Failed to delete skill: {exc}") from exc

        self._invalidate()
        return {
            "id": skill_id,
            "name": info["name"],
            "description": info["description"],
            "enabled": info["enabled"],
        }

    # ------------------------------------------------------------------
    # File management
    # ------------------------------------------------------------------

    def _resolve_skill(self, skill_id: str) -> tuple[str, Path]:
        """Normalise *skill_id* and resolve its directory.

        Returns ``(actual_id, resolved_path)``.  Accepts both enabled and
        disabled folder names (with/without leading dot).

        Raises ``SkillNotFoundError`` if the skill does not exist.
        """
        self._maybe_refresh()
        actual_id = skill_id
        if actual_id not in self._cache:
            alt = f".{skill_id}" if not skill_id.startswith(".") else skill_id.lstrip(".")
            if alt in self._cache:
                actual_id = alt
            else:
                raise SkillNotFoundError(skill_id)
        return actual_id, self._safe_resolve(actual_id)

    def list_files(self, skill_id: str) -> list[dict]:
        """Return a flat listing of all files under the skill directory.

        Each entry: ``{name, path, type, size}``.
        Directories have ``size=0``.
        Hidden files (dot-prefix) and the leading dot for disabled skills
        are NOT exposed — paths are relative to the skill root regardless
        of enabled/disabled state.
        """
        _id, skill_dir = self._resolve_skill(skill_id)
        entries: list[dict] = []
        for item in sorted(skill_dir.rglob("*")):
            rel = item.relative_to(skill_dir)
            # Skip hidden items
            if any(part.startswith(".") for part in rel.parts):
                continue
            entries.append({
                "name": rel.name,
                "path": str(rel),
                "type": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0,
            })
        return entries

    def read_file(self, skill_id: str, file_path: str) -> dict:
        """Read a file inside a skill directory.

        Returns ``{content, hash, size, last_modified}``.
        """
        _id, skill_dir = self._resolve_skill(skill_id)
        target = self._safe_join(skill_dir, file_path)

        if not target.exists():
            raise SkillError(f"File not found: {file_path}")
        if not target.is_file():
            raise SkillError(f"Not a file: {file_path}")
        if _is_binary(target):
            raise SkillError(f"Cannot read binary file: {file_path}")

        size = target.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise SkillError(f"File too large ({size} bytes, max {_MAX_FILE_BYTES}): {file_path}")

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SkillError(f"Failed to read {file_path}: {exc}") from exc

        return {
            "content": content,
            "hash": hashlib.sha256(content.encode()).hexdigest(),
            "size": size,
            "last_modified": _mtime_to_iso(target.stat().st_mtime),
        }

    def write_file(
        self,
        skill_id: str,
        file_path: str,
        content: str,
        expected_hash: str | None = None,
    ) -> dict:
        """Write content to a file inside a skill directory.

        If *file_path* is ``SKILL.md``, the content is validated for valid
        YAML frontmatter with non-empty ``name`` and ``description``.

        Returns ``{path, hash, size, last_modified}``.

        Raises ``SkillError`` on validation failure or hash conflict.
        """
        _id, skill_dir = self._resolve_skill(skill_id)
        self._validate_file_path(file_path)
        target = self._safe_join(skill_dir, file_path)

        # SKILL.md validation
        basename = Path(file_path).name
        if basename == _SKILL_MD:
            valid, err = self._validate_skill_md(content)
            if not valid:
                raise SkillError(f"Invalid SKILL.md: {err}")

        # Hash conflict check for existing files
        if target.exists() and expected_hash:
            current = target.read_text(encoding="utf-8", errors="replace")
            current_hash = hashlib.sha256(current.encode()).hexdigest()
            if current_hash != expected_hash:
                raise SkillError(
                    f"File has been modified externally. "
                    f"Expected hash {expected_hash[:8]}… but got {current_hash[:8]}…"
                )

        # Ensure parent directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            atomic_write_text(target, content)
        except OSError as exc:
            raise SkillError(f"Failed to write {file_path}: {exc}") from exc

        # Invalidate cache if SKILL.md was modified
        if basename == _SKILL_MD:
            self._invalidate()

        return {
            "path": file_path,
            "hash": hashlib.sha256(content.encode()).hexdigest(),
            "size": len(content.encode("utf-8")),
            "last_modified": _mtime_to_iso(target.stat().st_mtime),
        }

    def create_file(self, skill_id: str, path: str, item_type: str) -> dict:
        """Create a new file or directory inside a skill directory.

        Returns ``{path, type}``.
        """
        if item_type not in ("file", "directory"):
            raise SkillError(f"Invalid type: {item_type!r}. Must be 'file' or 'directory'.")

        _id, skill_dir = self._resolve_skill(skill_id)
        self._validate_file_path(path)
        target = self._safe_join(skill_dir, path)

        if target.exists():
            raise SkillError(f"Already exists: {path}")

        try:
            if item_type == "directory":
                target.mkdir(parents=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(target, "")
        except OSError as exc:
            raise SkillError(f"Failed to create {path}: {exc}") from exc

        return {"path": path, "type": item_type}

    def rename_file(self, skill_id: str, path: str, new_name: str) -> dict:
        """Rename a file or directory inside a skill directory.

        Renaming ``SKILL.md`` is forbidden (it would break skill discovery).

        Returns ``{old_path, new_path}``.
        """
        _id, skill_dir = self._resolve_skill(skill_id)
        self._validate_file_path(path)
        target = self._safe_join(skill_dir, path)

        if not target.exists():
            raise SkillError(f"Not found: {path}")

        # Protect SKILL.md
        if Path(path).name == _SKILL_MD:
            raise SkillError("Cannot rename SKILL.md — it is required for skill discovery")

        self._validate_filename(new_name)
        new_target = self._safe_join(skill_dir, str(Path(path).parent / new_name))

        if new_target.exists():
            raise SkillError(f"Target already exists: {Path(path).parent / new_name}")

        try:
            target.rename(new_target)
        except OSError as exc:
            raise SkillError(f"Failed to rename {path}: {exc}") from exc

        new_path = str(Path(path).parent / new_name) if str(Path(path).parent) != "." else new_name
        return {"old_path": path, "new_path": new_path}

    def delete_file(self, skill_id: str, file_path: str) -> dict:
        """Delete a file or *empty* directory inside a skill directory.

        Deleting ``SKILL.md`` is forbidden.

        Returns ``{path}``.
        """
        _id, skill_dir = self._resolve_skill(skill_id)
        self._validate_file_path(file_path)
        target = self._safe_join(skill_dir, file_path)

        if not target.exists():
            raise SkillError(f"Not found: {file_path}")

        # Protect SKILL.md
        if Path(file_path).name == _SKILL_MD:
            raise SkillError("Cannot delete SKILL.md — it is required for skill discovery")

        try:
            if target.is_dir():
                # Only delete empty directories to prevent accidental data loss
                if any(target.iterdir()):
                    raise SkillError(f"Directory not empty: {file_path}")
                target.rmdir()
            else:
                target.unlink()
        except OSError as exc:
            raise SkillError(f"Failed to delete {file_path}: {exc}") from exc

        return {"path": file_path}

    # ------------------------------------------------------------------
    # Skill import — ZIP upload & Git clone
    # ------------------------------------------------------------------

    async def import_zip(self, upload_file: Any) -> dict:
        """Import skills from an uploaded ZIP archive.

        The ZIP is extracted to a temporary directory, scanned for valid
        skill directories (containing ``SKILL.md`` with valid frontmatter),
        and imported in disabled state.

        Returns ``{imported: [...], skipped: [...]}``.
        """
        tmp_dir = Path(tempfile.mkdtemp(prefix="sigma-skill-import-"))
        try:
            zip_path = tmp_dir / "upload.zip"

            # Save uploaded file with size limit
            total = 0
            with open(zip_path, "wb") as f:
                while True:
                    chunk = await upload_file.read(1024 * 1024)  # 1 MB chunks
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_ZIP_SIZE:
                        raise SkillError(
                            f"ZIP file too large (>{_MAX_ZIP_SIZE // 1_000_000} MB)"
                        )
                    f.write(chunk)

            # Extract with security checks
            extract_dir = tmp_dir / "extracted"
            self._safe_extract_zip(zip_path, extract_dir)

            return self._import_from_dir(extract_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def import_git(self, url: str) -> dict:
        """Import skills from a Git repository.

        The repository is shallow-cloned to a temporary directory, scanned
        for valid skill directories, and imported in disabled state.

        Returns ``{imported: [...], skipped: [...]}``.
        """
        self._validate_git_url(url)

        tmp_dir = Path(tempfile.mkdtemp(prefix="sigma-skill-import-"))
        try:
            clone_dir = tmp_dir / "repo"

            import asyncio
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth", "1", url, str(clone_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_CLONE_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                raise SkillError(
                    f"Git clone timed out after {_CLONE_TIMEOUT}s"
                )

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip()
                raise SkillError(f"Git clone failed: {err_msg}")

            return self._import_from_dir(clone_dir)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _import_from_dir(self, source_dir: Path) -> dict:
        """Scan a directory for valid skills and import them.

        Finds every ``SKILL.md`` in the tree, validates its frontmatter,
        and copies the containing directory into the skill store in
        disabled state (dot-prefix).  Name collisions are resolved by
        appending an 8-char random suffix.

        Returns ``{imported: [...], skipped: [...]}``.
        """
        imported: list[dict] = []
        skipped: list[dict] = []
        seen_dirs: set[Path] = set()  # avoid importing the same dir twice

        for skill_md in source_dir.rglob(_SKILL_MD):
            skill_dir = skill_md.parent

            # Skip if we already imported this directory (nested SKILL.md)
            if skill_dir in seen_dirs:
                continue

            rel = skill_dir.relative_to(source_dir)

            # Skip hidden paths
            if any(p.startswith(".") for p in rel.parts):
                continue

            # Read and validate SKILL.md
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped.append({"path": str(rel), "reason": "Cannot read SKILL.md"})
                continue

            parsed = _parse_frontmatter(text)
            name = parsed.get("name", "").strip()
            desc = parsed.get("description", "").strip()
            if not name or not desc:
                skipped.append({
                    "path": str(rel),
                    "reason": "SKILL.md missing required 'name' or 'description'",
                })
                continue

            # Determine target directory name
            dir_name = rel.name if str(rel) != "." else _slugify(name)
            target_name = f".{dir_name}"  # disabled

            # Resolve name collision with 8-char random suffix
            target = self._skills_dir / target_name
            if target.exists():
                target_name = f".{dir_name}-{_short_id()}"
                target = self._skills_dir / target_name

            # Copy directory
            try:
                shutil.copytree(skill_dir, target)
            except OSError as exc:
                skipped.append({
                    "path": str(rel),
                    "reason": f"Failed to copy: {exc}",
                })
                continue

            seen_dirs.add(skill_dir)
            imported.append({
                "id": target_name,
                "name": name,
                "description": desc,
            })

        self._invalidate()
        return {"imported": imported, "skipped": skipped}

    # ------------------------------------------------------------------
    # Import helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
        """Extract a ZIP archive safely, enforcing size and path limits."""
        total_size = 0
        entry_count = 0

        with zipfile.ZipFile(zip_path, "r") as zf:
            for info in zf.infolist():
                entry_count += 1
                if entry_count > _MAX_EXTRACT_ENTRIES:
                    raise SkillError(
                        f"ZIP contains too many entries (>{_MAX_EXTRACT_ENTRIES})"
                    )

                # Security: reject symlinks (Unix symlink bit = 0xA000)
                unix_attrs = info.external_attr >> 16
                if unix_attrs & 0o170000 == 0o120000:
                    raise SkillError(f"ZIP contains a symlink: {info.filename}")

                # Security: reject absolute paths and traversal
                member = info.filename.replace("\\", "/")
                if member.startswith("/") or ":/" in member:
                    raise SkillError(f"Absolute path in ZIP: {info.filename}")
                parts = Path(member).parts
                if ".." in parts:
                    raise SkillError(f"Path traversal in ZIP: {info.filename}")

                # Security: reject hidden entries
                if any(p.startswith(".") for p in parts):
                    continue

                # Size checks
                if info.file_size > _MAX_EXTRACT_PER_FILE:
                    raise SkillError(
                        f"File too large in ZIP: {info.filename} "
                        f"({info.file_size} bytes, limit {_MAX_EXTRACT_PER_FILE})"
                    )
                total_size += info.file_size
                if total_size > _MAX_EXTRACT_TOTAL:
                    raise SkillError("ZIP total size exceeds limit")

                # Extract (zipfile.extract already sanitises paths in Python 3.12+,
                # but we checked manually above for older versions)
                zf.extract(info, dest)

    @staticmethod
    def _validate_git_url(url: str) -> None:
        """Reject non-HTTP(S) Git URLs to prevent protocol abuse."""
        try:
            parsed = urlparse(url)
        except Exception:
            raise SkillError(f"Invalid URL: {url}")

        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise SkillError(
                f"Only http:// and https:// URLs are allowed, got: {scheme}://"
            )

        if not parsed.hostname:
            raise SkillError(f"Invalid URL (no hostname): {url}")

        # Reject obvious injection patterns
        if any(c in url for c in (";", "|", "`", "$", "(", ")")):
            raise SkillError("Invalid characters in URL")

    # ------------------------------------------------------------------
    # File-management helpers (path & content validation)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_filename(name: str) -> None:
        """Reject obviously invalid or dangerous filenames."""
        if not name or not name.strip():
            raise SkillError("Filename cannot be empty")
        if name.startswith("."):
            raise SkillError("Hidden files (dot-prefix) are not allowed")
        if "/" in name or "\\" in name or ".." in name:
            raise SkillError(f"Invalid filename: {name}")

    @staticmethod
    def _validate_file_path(path: str) -> None:
        """Reject paths with hidden components or traversal attempts."""
        parts = Path(path).parts
        for part in parts:
            if part.startswith("."):
                raise SkillError(f"Hidden path component not allowed: {part}")
            if part in ("..",):
                raise SkillError("Path traversal not allowed")

    @staticmethod
    def _validate_skill_md(content: str) -> tuple[bool, str]:
        """Validate that ``SKILL.md`` content has valid frontmatter.

        Returns ``(is_valid, error_message)``.
        """
        parsed = _parse_frontmatter(content)
        errors: list[str] = []
        if "name" not in parsed or not parsed["name"].strip():
            errors.append("missing or empty 'name' in YAML frontmatter")
        if "description" not in parsed or not parsed["description"].strip():
            errors.append("missing or empty 'description' in YAML frontmatter")
        if errors:
            return False, "; ".join(errors)
        return True, ""

    def build_skills_prompt(self) -> str:
        """Build the ``<skills>`` XML block for system prompt injection.

        Only *enabled* skills are included.  Descriptions are truncated to
        ``_MAX_DESC_LEN`` chars and the total block is capped at
        ``_MAX_PROMPT_CHARS`` chars.
        """
        self._maybe_refresh()

        enabled = [
            (sid, info)
            for sid, info in self._cache.items()
            if info["enabled"]
        ]

        if not enabled:
            return ""

        lines: list[str] = ["<skills>"]
        total_len = len(lines[0]) + 1  # + newline

        for sid, info in enabled:
            desc = info["description"][:_MAX_DESC_LEN]
            line = (
                f'<skill><id>{sid}</id>'
                f'<name>{info["name"]}</name>'
                f'<description>{desc}</description></skill>'
            )
            if total_len + len(line) + len("</skills>") + 50 > _MAX_PROMPT_CHARS:
                break  # stop adding skills to stay within budget
            lines.append(line)
            total_len += len(line) + 1

        lines.append("</skills>")

        instruction = (
            "\nBefore replying, scan the skills listed above. If a skill matches "
            "or is even partially relevant to your task, you MUST load it with "
            'skill_load tool and follow its instructions. '
            "After loading, strictly adhere to the skill's Markdown instructions. "
            "Do not guess skill content — always load first. "
            "Do not load the same skill more than once in a conversation."
        )
        lines.append(instruction)

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cache internals
    # ------------------------------------------------------------------

    def _invalidate(self) -> None:
        """Clear the in-memory cache."""
        self._cache.clear()
        self._dir_mtime = 0.0

    def _maybe_refresh(self) -> None:
        """Refresh cache if the skills directory has changed."""
        try:
            current_mtime = self._skills_dir.stat().st_mtime
        except OSError:
            self._cache.clear()
            return

        if self._cache and current_mtime == self._dir_mtime:
            return  # cache is fresh

        self._dir_mtime = current_mtime
        self._cache.clear()

        try:
            entries = list(self._skills_dir.iterdir())
        except OSError:
            return

        for entry in sorted(entries, key=lambda e: e.name):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") and entry.name in (".", ".."):
                continue
            # Skip hidden dirs that are not skill dirs (e.g. .hub)
            # We treat dot-prefix as disabled skills
            skill_md = entry / _SKILL_MD
            if not skill_md.is_file():
                continue

            folder_name = entry.name
            enabled = not folder_name.startswith(".")

            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            parsed = _parse_frontmatter(text)
            # Both name and description are required for a valid skill
            if "name" not in parsed or "description" not in parsed:
                continue
            name = parsed["name"]
            description = parsed["description"]
            if not name.strip() or not description.strip():
                continue

            try:
                mtime = entry.stat().st_mtime
            except OSError:
                mtime = 0.0

            self._cache[folder_name] = {
                "name": name,
                "description": description,
                "enabled": enabled,
                "mtime": mtime,
            }

    # ------------------------------------------------------------------
    # Path security
    # ------------------------------------------------------------------

    def _safe_resolve(self, skill_id: str) -> Path:
        """Resolve a skill_id to an absolute directory, verifying it is under
        ``self._skills_dir``.  Rejects ``..`` components and symlink escapes."""
        # Basic sanitisation
        if ".." in skill_id or "/" in skill_id or "\\" in skill_id:
            raise SkillError(f"Invalid skill id: {skill_id}")

        resolved = (self._skills_dir / skill_id).resolve()
        root = self._skills_dir.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise SkillError(f"Invalid skill path: {skill_id}")
        if not resolved.is_dir():
            raise SkillNotFoundError(skill_id)
        return resolved

    @staticmethod
    def _safe_join(root: Path, relative: str) -> Path:
        """Join and verify the result stays within *root*.

        Strips leading slashes so absolute-ish paths are treated as relative.
        """
        cleaned = relative.lstrip("/")
        full = (root / cleaned).resolve()
        root_resolved = root.resolve()
        if full != root_resolved and not full.is_relative_to(root_resolved):
            raise SkillError("Path traversal attempt detected")
        # Also check for symlink escape
        if full.is_symlink():
            real_target = full.resolve()
            if real_target != root_resolved and not real_target.is_relative_to(root_resolved):
                raise SkillError("Symlink escape detected")
        return full


# Singleton
skill_service = SkillService()
