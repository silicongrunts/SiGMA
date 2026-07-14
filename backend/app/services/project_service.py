"""
Project Service — project CRUD, metadata, config, and delete orchestration.

Owns projects.json (atomic R-M-W) and per-project DB config.
Routes and services call this module for project lifecycle operations.
"""

import io
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import List, Dict, Any, Optional

from app.core.config import settings
from app.core.document_status import ACTIVE_STATUSES
from app.core.utils import is_within, to_iso, utcnow
from app.core.atomic_file import ProjectFileLock, atomic_write_json, safe_read_json
from app.core.exceptions import FileSystemError, ProjectNotFoundError
from app.core.logging import get_logger
from app.services.git_service import git_service

logger = get_logger(__name__)

PROJECT_STATUS_ACTIVE = "active"
PROJECT_STATUS_RESETTING = "resetting"
PROJECT_STATUS_DELETING = "deleting"
PROJECT_STATUS_DELETED = "deleted"


class ProjectService:
    """Project lifecycle: create, read, update, delete, config."""

    _CONFIG_KEYS = frozenset({"main_file", "engine", "template"})

    def __init__(self):
        self.USERDATA_DIR = settings.USERDATA_DIR.resolve()
        self.SIGMA_DIR = self.USERDATA_DIR / ".SiGMA"
        self.PROJECTS_FILE = self.SIGMA_DIR / "projects.json"

        self.USERDATA_DIR.mkdir(parents=True, exist_ok=True)
        self.SIGMA_DIR.mkdir(parents=True, exist_ok=True)
        if not self.PROJECTS_FILE.exists():
            self.PROJECTS_FILE.write_text("{}", encoding='utf-8')

    # ------------------------------------------------------------------
    # Project path helpers
    # ------------------------------------------------------------------

    def get_project_path(self, project_id: str) -> Path:
        """Resolve and validate a project directory path."""
        path = (self.USERDATA_DIR / project_id).resolve()
        if (
            not path.exists()
            or not is_within(path, self.USERDATA_DIR)
            or not self.is_project_active(project_id)
        ):
            raise ProjectNotFoundError(project_id)
        return path

    # ------------------------------------------------------------------
    # projects.json atomic read-modify-write
    # ------------------------------------------------------------------

    def _update_projects(self, mutator):
        """Read-modify-write projects.json under a single lock.

        *mutator* receives the current dict and should modify it in-place
        and/or return a result value.

        Raises ``FileSystemError(CORRUPT_PROJECT_INDEX)`` on corrupt JSON.
        """
        with ProjectFileLock(self.PROJECTS_FILE):
            try:
                projects = safe_read_json(self.PROJECTS_FILE)
            except FileNotFoundError:
                projects = {}
            except (json.JSONDecodeError, ValueError) as exc:
                self._handle_corrupt_projects(exc)
                raise FileSystemError(
                    "Project index file is corrupt and has been backed up. "
                    "Please restore from backup or recreate the index.",
                    code="CORRUPT_PROJECT_INDEX",
                ) from exc
            result = mutator(projects)
            atomic_write_json(self.PROJECTS_FILE, projects)
            return result

    def _handle_corrupt_projects(self, exc: Exception) -> None:
        """Backup a corrupt projects.json and log the event."""
        logger.error("projects.json is corrupt: %s", exc)
        try:
            ts = utcnow().strftime("%Y%m%d_%H%M%S")
            corrupt_path = self.PROJECTS_FILE.with_name(f"projects.json.corrupt.{ts}")
            self.PROJECTS_FILE.rename(corrupt_path)
            logger.info("Corrupt file backed up to %s", corrupt_path)
        except OSError as e:
            logger.error("Failed to backup corrupt projects.json: %s", e)

    def _load_projects_readonly(self) -> Dict[str, Any]:
        """Read projects.json for read-only access.

        Raises typed exception on corruption.
        """
        try:
            with ProjectFileLock(self.PROJECTS_FILE):
                return safe_read_json(self.PROJECTS_FILE)
        except (json.JSONDecodeError, ValueError) as exc:
            self._handle_corrupt_projects(exc)
            raise FileSystemError(
                "Project index file is corrupt and has been backed up. "
                "Please check your data directory.",
                code="CORRUPT_PROJECT_INDEX",
            ) from exc

    def _get_project_entry(self, project_id: str) -> Dict[str, Any] | None:
        projects = self._load_projects_readonly()
        entry = projects.get(project_id)
        return entry if isinstance(entry, dict) else None

    @staticmethod
    def _status_of(entry: Dict[str, Any]) -> str:
        return entry.get("status") or PROJECT_STATUS_ACTIVE

    def is_project_active(self, project_id: str) -> bool:
        """Return True only when the global registry allows project work.

        Delegates the raw registry read to ``core.project_registry`` so
        that lower layers (DB manager, workers) can consult the same
        source without depending on this service.
        """
        from app.core.project_registry import is_project_active as _is_active
        return _is_active(project_id)

    def get_project_meta(self, project_id: str) -> Dict[str, str]:
        """Return ``{"name": ..., "description": ...}`` from the registry.

        Lightweight read-only lookup used for prompt rendering. Returns
        empty strings when the project is missing so callers can render
        prompts without a registry entry (e.g. mid-deletion).
        """
        entry = self._get_project_entry(project_id) or {}
        return {
            "name": entry.get("name") or "",
            "description": entry.get("description") or "",
        }

    def mark_project_deleting(self, project_id: str) -> None:
        """Persist the deletion barrier before task cleanup begins."""
        now = to_iso(utcnow())

        def _mark(projects):
            entry = projects.get(project_id)
            if not isinstance(entry, dict):
                raise ProjectNotFoundError(project_id)
            entry["status"] = PROJECT_STATUS_DELETING
            entry["deleted_at"] = now
            entry["modified"] = now

        self._update_projects(_mark)

    def mark_project_resetting(self, project_id: str) -> None:
        """Persist a short-lived barrier while the project DB is reset."""
        now = to_iso(utcnow())

        def _mark(projects):
            entry = projects.get(project_id)
            if not isinstance(entry, dict) or self._status_of(entry) != PROJECT_STATUS_ACTIVE:
                raise ProjectNotFoundError(project_id)
            entry["status"] = PROJECT_STATUS_RESETTING
            entry["modified"] = now

        self._update_projects(_mark)

    def mark_project_active(self, project_id: str) -> None:
        """Restore normal project access after a transient lifecycle action."""
        now = to_iso(utcnow())

        def _mark(projects):
            entry = projects.get(project_id)
            if not isinstance(entry, dict):
                raise ProjectNotFoundError(project_id)
            if self._status_of(entry) == PROJECT_STATUS_DELETED:
                raise ProjectNotFoundError(project_id)
            entry["status"] = PROJECT_STATUS_ACTIVE
            entry["modified"] = now

        self._update_projects(_mark)

    def mark_project_deleted(self, project_id: str) -> None:
        """Persist the final deleted state after best-effort cleanup."""
        now = to_iso(utcnow())

        def _mark(projects):
            entry = projects.get(project_id)
            if not isinstance(entry, dict):
                return
            entry["status"] = PROJECT_STATUS_DELETED
            entry["deleted_at"] = entry.get("deleted_at") or now
            entry["modified"] = now

        self._update_projects(_mark)

    # ------------------------------------------------------------------
    # Per-project config helpers (project_config table)
    # ------------------------------------------------------------------

    async def _get_config(self, project_id: str) -> Dict[str, str]:
        """Read config keys from the project's DB.

        Failures are non-fatal: if the DB is missing, corrupt, or has an
        incompatible schema revision, defaults are returned so that
        ``list_projects`` can still show the project rather than failing
        the entire list.
        """
        from app.database.unit_of_work import UnitOfWork
        from sqlalchemy.exc import OperationalError
        defaults = {"main_file": "", "engine": settings.DEFAULT_LATEX_ENGINE, "template": "latex"}
        try:
            async with UnitOfWork(project_id) as uow:
                for key in self._CONFIG_KEYS:
                    val = await uow.config.get(key, None)
                    if val is not None:
                        defaults[key] = val
        except OperationalError:
            # Project DB not initialized yet — return defaults unchanged
            logger.debug("Project DB not initialized for %s", project_id, exc_info=True)
        except Exception as exc:
            # Corrupt DB, incompatible revision, disk error, etc. — log
            # and return defaults so one bad project doesn't kill the list.
            logger.warning("Project DB unreadable for %s: %s", project_id, exc, exc_info=True)
        return defaults

    async def _set_config(self, project_id: str, config: Dict[str, str]) -> None:
        """Write config keys to the project's DB."""
        from app.database.unit_of_work import UnitOfWork
        async with UnitOfWork(project_id) as uow:
            for key in self._CONFIG_KEYS:
                if key in config:
                    await uow.config.set(key, config[key])

    async def get_project_config(self, project_id: str) -> Dict[str, Any]:
        """Get project configuration (snapshot settings, tips, etc.)."""
        from app.database.unit_of_work import UnitOfWork
        async with UnitOfWork(project_id) as uow:
            all_config = await uow.config.get_all()

        result = {
            "snapshot_enabled": True,
            "snapshot_interval_minutes": 5,
            "tips": "",
        }
        if "snapshot_enabled" in all_config:
            result["snapshot_enabled"] = all_config["snapshot_enabled"] == "true"
        if "snapshot_interval_minutes" in all_config:
            try:
                result["snapshot_interval_minutes"] = int(all_config["snapshot_interval_minutes"])
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid snapshot_interval_minutes value %r for project %s, using default",
                    all_config["snapshot_interval_minutes"], project_id,
                )
        if "tips" in all_config:
            result["tips"] = all_config["tips"]
        return result

    async def update_project_config(self, project_id: str, data) -> None:
        """Update project configuration from a ProjectConfigUpdate schema."""
        from app.database.unit_of_work import UnitOfWork
        async with UnitOfWork(project_id) as uow:
            if data.snapshot_enabled is not None:
                await uow.config.set("snapshot_enabled", "true" if data.snapshot_enabled else "false")
            if data.snapshot_interval_minutes is not None:
                await uow.config.set("snapshot_interval_minutes", str(data.snapshot_interval_minutes))
            if data.tips is not None:
                await uow.config.set("tips", data.tips)

    # ------------------------------------------------------------------
    # Permission auto-approve (project_config keys: auto_approve.<category>)
    # ------------------------------------------------------------------

    async def get_auto_approve(self, project_id: str) -> Dict[str, bool]:
        """Return the four-category auto-approve flags for a project."""
        from app.database.unit_of_work import UnitOfWork
        from app.services.permission_executor import PERMISSION_CATEGORIES
        result = {cat: False for cat in PERMISSION_CATEGORIES}
        try:
            async with UnitOfWork(project_id) as uow:
                for cat in PERMISSION_CATEGORIES:
                    val = await uow.config.get(f"auto_approve.{cat}", "false")
                    result[cat] = val == "true"
        except Exception:
            # Project DB not ready — return all-off defaults (safest).
            logger.debug("Auto-approve read failed for %s", project_id, exc_info=True)
        return result

    async def set_auto_approve(self, project_id: str, category: str, enabled: bool) -> None:
        """Toggle one auto-approve category. ``category`` validity is the
        caller's responsibility (the route validates against the schema)."""
        from app.database.unit_of_work import UnitOfWork
        async with UnitOfWork(project_id) as uow:
            await uow.config.set(
                f"auto_approve.{category}", "true" if enabled else "false",
            )

    # ------------------------------------------------------------------
    # Template discovery
    # ------------------------------------------------------------------

    def list_templates(self) -> List[Dict]:
        """Scan the user template directory for available project templates."""
        tpl_dir = self.SIGMA_DIR / "templates"

        results = {}
        if tpl_dir.is_dir():
            for d in tpl_dir.iterdir():
                if not d.is_dir():
                    continue
                meta_file = d / "template.json"
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    except Exception:
                        logger.debug("Failed to read template metadata %s", meta_file, exc_info=True)
                        meta = {}
                else:
                    meta = {}
                if d.name not in results:
                    results[d.name] = {
                        "id": d.name,
                        "name": meta.get("name", d.name.title()),
                        "icon": meta.get("icon", d.name[:2].upper()),
                        "desc": meta.get("desc", ""),
                        "main_file": meta.get("main_file", ""),
                    }
        return sorted(results.values(), key=lambda x: x["name"])

    # ------------------------------------------------------------------
    # Project CRUD
    # ------------------------------------------------------------------

    async def list_projects(self) -> List[Dict[str, Any]]:
        projects_data = self._load_projects_readonly()
        result = []
        for pid, info in projects_data.items():
            if not isinstance(info, dict) or self._status_of(info) != PROJECT_STATUS_ACTIVE:
                continue
            if not (self.USERDATA_DIR / pid).is_dir():
                continue
            config = await self._get_config(pid)
            result.append({
                "id": pid,
                "name": info.get("name", pid),
                "description": info.get("description", ""),
                "main_file": config.get("main_file", ""),
                "engine": config.get("engine", settings.DEFAULT_LATEX_ENGINE),
                "template": config.get("template", "latex"),
                "created": info.get("created"),
                "modified": info.get("modified"),
            })
        return result

    async def create_project(self, name: str, description: str = "", template: str = "latex") -> Dict[str, Any]:
        project_id = uuid.uuid4().hex[:8]
        project_path = self.USERDATA_DIR / project_id
        project_path.mkdir(parents=True, exist_ok=True)

        # Resolve template directory
        src_dir = self.SIGMA_DIR / "templates" / template
        if not src_dir.is_dir():
            src_dir = None

        main_file = ""
        if src_dir:
            meta_file = src_dir / "template.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    main_file = meta.get("main_file", "")
                except Exception:
                    logger.debug("Failed to read template metadata %s", meta_file, exc_info=True)
            for f in src_dir.iterdir():
                if f.name != "template.json":
                    shutil.copy2(f, project_path / f.name)

        return await self._finalize_new_project(
            project_id, name, description,
            init_git=True,
            config={
                "main_file": main_file,
                "engine": settings.DEFAULT_LATEX_ENGINE,
                "template": template,
            },
        )

    async def import_project(
        self,
        name: str,
        description: str,
        zip_bytes: bytes,
        *,
        max_bytes: int,
    ) -> Dict[str, Any]:
        """Create a new project by extracting a user-uploaded zip archive.

        The caller derives ``name`` from the zip filename. The archive may
        contain files at the root or wrap them in a single top-level
        directory; the wrapper is stripped when unambiguous.

        ``.SiGMA/`` presence is the SiGMA-project signal:
          - Present  → ``.git/`` and ``.SiGMA/`` are preserved as-is. If the
            bundled DB is incompatible, the project is still imported and the
            UI can offer a database reset.
          - Absent   → ``.git/`` is discarded; a fresh history is initialized.

        Extraction is two-phase: members land in a sibling ``.tmp``
        directory first, and only after full validation is the directory
        atomically renamed into place. Any failure cleans up the partial
        extraction so the project index never references a half-written dir.
        """
        project_id = uuid.uuid4().hex[:8]
        project_path = self.USERDATA_DIR / project_id
        tmp_path = self.USERDATA_DIR / f".{project_id}.import.tmp"

        # Phase 1: extract into tmp_path.
        try:
            has_sigma = await self._extract_zip_to_dir(
                zip_bytes, tmp_path, max_bytes=max_bytes,
            )
        except BaseException:
            shutil.rmtree(tmp_path, ignore_errors=True)
            raise

        # Phase 2: atomic commit + finalize.
        try:
            os.replace(tmp_path, project_path)
        except OSError as exc:
            shutil.rmtree(tmp_path, ignore_errors=True)
            raise FileSystemError(
                f"Failed to commit imported project: {exc}",
                code="IMPORT_COMMIT_FAILED",
            ) from exc

        # No SiGMA DB to read main_file from — pick a sensible default.
        config = None
        if not has_sigma:
            config = {
                "main_file": self._detect_main_file(project_path),
                "engine": settings.DEFAULT_LATEX_ENGINE,
                "template": "imported",
            }

        return await self._finalize_new_project(
            project_id, name, description,
            # SiGMA project keeps its existing .git (snapshot history);
            # everything else gets a fresh init (external .git already
            # discarded during extraction).
            init_git=not has_sigma,
            config=config,
        )

    async def register_project(self, directory: str, description: str = "") -> Dict[str, Any]:
        """Register a directory the user copied manually into ``userdata/``.

        Used as the fallback path when a project is too large to upload as a
        zip. The directory is moved to a fresh ``project_id`` slot and run
        through the same finalization as ``import_project`` so the SiGMA /
        non-SiGMA rules and DB migration behave identically.

        Refuses the protected ``.SiGMA`` registry, anything already
        registered, anything that does not exist, and any name containing a
        path separator.
        """
        # Reject path-bearing names before touching the filesystem. Also
        # reject ``.`` / ``..`` because they resolve to ``USERDATA_DIR``
        # itself and would let a rename move the whole userdata root.
        if not directory or "/" in directory or "\\" in directory or directory != directory.strip():
            raise FileSystemError(
                f"Invalid directory name: {directory!r}",
                code="INVALID_DIRECTORY_NAME",
            )
        if directory in (".", "..") or Path(directory).name != directory:
            raise FileSystemError(
                f"Refused directory name: {directory!r}",
                code="INVALID_DIRECTORY_NAME",
            )
        if directory == ".SiGMA":
            raise FileSystemError(
                "The .SiGMA directory is the project registry and cannot be registered",
                code="PROTECTED_DIRECTORY",
            )

        src = (self.USERDATA_DIR / directory).resolve()
        if not is_within(src, self.USERDATA_DIR) or not src.is_dir():
            raise FileSystemError(
                f"Directory not found in userdata: {directory!r}",
                code="DIRECTORY_NOT_FOUND",
            )

        # Reject anything that is already a known project (by current id or
        # by leftover project_data.db pointing back here).
        registered_ids = set(self._load_projects_readonly().keys())
        if directory in registered_ids:
            raise FileSystemError(
                f"Directory {directory!r} is already registered",
                code="ALREADY_REGISTERED",
            )

        project_id = uuid.uuid4().hex[:8]
        has_sigma = (src / ".SiGMA").is_dir()

        # Rename FIRST so a failed rename leaves the user's original
        # directory intact. Destructive .git cleanup happens on the renamed
        # copy, never on the source.
        try:
            os.replace(src, self.USERDATA_DIR / project_id)
        except OSError as exc:
            raise FileSystemError(
                f"Failed to register directory: {exc}",
                code="REGISTER_FAILED",
            ) from exc

        project_path = self.USERDATA_DIR / project_id

        # Discard external .git for non-SiGMA imports — same rule as zip import.
        if not has_sigma:
            ext_git = project_path / ".git"
            if ext_git.exists():
                shutil.rmtree(ext_git, ignore_errors=True)

        config = None
        if not has_sigma:
            config = {
                "main_file": self._detect_main_file(project_path),
                "engine": settings.DEFAULT_LATEX_ENGINE,
                "template": "imported",
            }

        return await self._finalize_new_project(
            project_id, self.sanitize_import_name(directory), description,
            init_git=not has_sigma,
            config=config,
        )

    def list_unregistered_dirs(self) -> List[Dict[str, Any]]:
        """List userdata subdirectories that are not registered as projects.

        Powers the "register a manually-copied directory" fallback UI.
        Skips the protected ``.SiGMA`` registry and any dir whose name is a
        known project id. Names that look like interrupted imports
        (``.<id>.import.tmp``) are also excluded — those are sweep targets.
        """
        if not self.USERDATA_DIR.exists():
            return []
        registered = set(self._load_projects_readonly().keys())
        result = []
        for entry in sorted(self.USERDATA_DIR.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if name == ".SiGMA" or name in registered:
                continue
            if name.startswith(".") and name.endswith(".import.tmp"):
                continue
            result.append({
                "name": name,
                "has_sigma": (entry / ".SiGMA").is_dir(),
            })
        return result

    def cleanup_interrupted_imports(self) -> int:
        """Remove leftover ``.<id>.import.tmp`` directories from userdata.

        Called at startup so an interrupted upload from a previous session
        does not leak disk space. Returns the number of directories removed.
        A missing or busy directory is logged and skipped.
        """
        if not self.USERDATA_DIR.exists():
            return 0
        removed = 0
        for entry in self.USERDATA_DIR.iterdir():
            if not entry.is_dir():
                continue
            if not (entry.name.startswith(".") and entry.name.endswith(".import.tmp")):
                continue
            try:
                shutil.rmtree(entry, ignore_errors=False)
                removed += 1
                logger.info("Cleaned up interrupted import: %s", entry.name)
            except OSError as exc:
                logger.warning(
                    "Could not remove interrupted import %s: %s",
                    entry.name, exc, exc_info=True,
                )
        return removed

    async def _finalize_new_project(
        self,
        project_id: str,
        name: str,
        description: str,
        *,
        init_git: bool,
        config: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Register and finalize a freshly populated project directory.

        Shared closing logic for ``create_project`` (template copy) and
        ``import_project`` (zip extract). The caller must have already
        populated ``userdata/<project_id>/`` with the project files.

        ``init_git`` runs ``git init`` when True (template / fresh import).
        When False, an existing ``.git`` is reused (SiGMA project import).

        ``config`` is written to the per-project DB after the schema is
        ensured. When None, an existing DB is migrated in place and its
        config values are preserved (SiGMA project import).
        """
        if init_git:
            try:
                git_service.init_git(project_id)
            except Exception:
                logger.warning("Git init failed for project %s", project_id, exc_info=True)

        # Ensure the project isn't blocked by a stale deletion flag.
        from app.database.manager import get_db_manager
        db_manager = await get_db_manager()
        db_manager.unmark_deleted(project_id)

        now = to_iso(utcnow())
        project_info = {
            "name": name,
            "description": description,
            "status": PROJECT_STATUS_ACTIVE,
            "created": now,
            "modified": now,
        }
        self._update_projects(lambda p: p.update({project_id: project_info}))

        if config is not None:
            await self._set_config(project_id, config)
        else:
            from app.core.exceptions import DatabaseIncompatibleError

            try:
                await db_manager.ensure_db_exists(project_id)
            except DatabaseIncompatibleError as exc:
                logger.warning(
                    "Imported project %s has an incompatible database: %s",
                    project_id, exc,
                    exc_info=True,
                )

        stored = await self._get_config(project_id)
        return {"id": project_id, **project_info, **stored}

    # ------------------------------------------------------------------
    # Zip import helpers
    # ------------------------------------------------------------------

    async def _extract_zip_to_dir(
        self, zip_bytes: bytes, dest: Path, *, max_bytes: int,
    ) -> bool:
        """Extract ``zip_bytes`` into ``dest``; return whether a SiGMA-project
        ``.SiGMA/`` directory was present at the (post-strip) root.

        Wrapper stripping: if every member shares a single top-level
        directory, that prefix is removed. Mixed top-level dir + files
        disables stripping so user files are never silently dropped.

        Safety (defense in depth, each layer independently sufficient):
          - Member names go through ``file_service.sanitize_member``.
          - Each target path is verified to resolve inside ``dest``.
          - Symlink members are skipped (defense vs symlink escapes).
          - Cumulative uncompressed size is bounded by ``max_bytes``.
          - External ``.git/`` members are skipped when the archive is not
            a SiGMA project — otherwise we'd carry an unknown git history
            and let it count against the size limit.

        Any violation aborts the whole extraction with ``FileSystemError``;
        callers are expected to clean up ``dest`` on failure.
        """
        # Imported here to avoid a service-startup cycle.
        from app.services.file_service import file_service

        dest.mkdir(parents=True, exist_ok=False)

        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes), 'r')
        except zipfile.BadZipFile as exc:
            raise FileSystemError(
                "Uploaded file is not a valid zip archive",
                code="INVALID_ZIP",
            ) from exc

        with zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if not members:
                raise FileSystemError(
                    "Uploaded zip archive contains no files",
                    code="EMPTY_ZIP",
                )

            # Reject unsafe raw names (traversal, absolute paths, embedded
            # separators) BEFORE prefix stripping can mask them.
            for m in members:
                if not file_service.sanitize_member(m.filename):
                    raise FileSystemError(
                        f"Refused unsafe zip entry: {m.filename!r}",
                        code="UNSAFE_ZIP_ENTRY",
                    )

            prefix = self._detect_wrapper_prefix([m.filename for m in members])

            # SiGMA-project detection drives both .git preservation and the
            # main_file source (DB vs detection).
            def _top(name: str) -> str:
                stripped = name[len(prefix):] if prefix else name
                return stripped.split('/', 1)[0]
            has_sigma = any(_top(m.filename) == ".SiGMA" for m in members)

            total = 0
            for m in members:
                # Skip symlinks — they could point outside the project dir.
                mode = (m.external_attr >> 16) & 0xFFFF
                if mode & 0o170000 == 0o120000:
                    continue

                # Discard external .git history for non-SiGMA imports.
                if _top(m.filename) == ".git" and not has_sigma:
                    continue

                clean = m.filename[len(prefix):] if prefix else m.filename
                clean = clean.lstrip("/")  # tolerate a stray leading slash

                target = dest / clean
                if not is_within(target.resolve(), dest.resolve()):
                    raise FileSystemError(
                        f"Refused zip entry that escapes the project directory: {m.filename!r}",
                        code="UNSAFE_ZIP_ENTRY",
                    )

                target.parent.mkdir(parents=True, exist_ok=True)
                # Stream-extract with a running byte count so a falsified
                # ``file_size`` header in a malicious/corrupt zip cannot
                # bypass the size limit (the header check alone is not
                # sufficient defense against a real zip bomb).
                written = 0
                budget = max_bytes - total
                with zf.open(m, 'r') as src, open(target, 'wb') as dst:
                    while True:
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > budget:
                            raise FileSystemError(
                                f"Unzipped content exceeds the {max_bytes // (1024 * 1024)} MiB limit",
                                code="ZIP_TOO_LARGE",
                            )
                        dst.write(chunk)
                total += written

        return has_sigma

    @staticmethod
    def _detect_wrapper_prefix(member_names: List[str]) -> str:
        """Return the top-level directory prefix to strip, or "" if none.

        A wrapper is detected iff every member is contained under the same
        single top-level directory. A top-level file (no slash) disables
        stripping — we never silently drop sibling files at the root.
        """
        if not member_names:
            return ""
        top: Optional[str] = None
        for raw in member_names:
            normalized = raw.replace('\\', '/')
            parts = normalized.split('/', 1)
            if len(parts) == 1:
                return ""
            if top is None:
                top = parts[0]
            elif top != parts[0]:
                return ""
        return f"{top}/" if top else ""

    @staticmethod
    def sanitize_import_name(filename: str) -> str:
        """Derive a project name from an uploaded zip filename.

        Strips the ``.zip`` suffix, removes path separators (project names
        are display labels, not paths), truncates to the 100-char limit,
        and falls back to a stable default for unusable input.
        """
        stem = filename or ""
        if stem.lower().endswith(".zip"):
            stem = stem[:-4]
        # Drop any path separators / NULs that could corrupt JSON or UI display.
        cleaned = "".join(ch for ch in stem if ch not in ("/\\:\0"))
        cleaned = cleaned.strip().strip(".")  # no leading/trailing dots
        if not cleaned:
            cleaned = "imported_project"
        return cleaned[:100]

    @staticmethod
    def _detect_main_file(project_path: Path) -> str:
        """Pick a default main file: ``main.tex`` at the project root if present."""
        candidate = project_path / "main.tex"
        if candidate.is_file() and is_within(candidate.resolve(), project_path.resolve()):
            return "main.tex"
        return ""

    async def get_project(self, project_id: str) -> Dict[str, Any]:
        info = self._get_project_entry(project_id)
        if not info or self._status_of(info) != PROJECT_STATUS_ACTIVE:
            raise ProjectNotFoundError(project_id)
        config = await self._get_config(project_id)

        # Check DB health so the frontend can prompt for a reset before the
        # user hits cryptic errors in chat/messages.
        db_status = await self._check_db_status(project_id)

        return {
            "id": project_id,
            "name": info.get("name"),
            "description": info.get("description", ""),
            "template": config.get("template", "latex"),
            "main_file": config.get("main_file", ""),
            "engine": config.get("engine", settings.DEFAULT_LATEX_ENGINE),
            "created": info.get("created"),
            "modified": info.get("modified"),
            "db_status": db_status,
        }

    async def _check_db_status(self, project_id: str) -> str:
        """Return 'ok', 'incompatible', or 'error' for the project's database."""
        from app.database.manager import get_db_manager
        from app.core.exceptions import DatabaseIncompatibleError
        db_path = settings.get_project_path(project_id) / ".SiGMA" / "project_data.db"
        if not db_path.exists():
            return "ok"  # new project — DB will be created on first access
        try:
            db_manager = await get_db_manager()
            await db_manager.ensure_db_exists(project_id)
            return "ok"
        except DatabaseIncompatibleError:
            return "incompatible"
        except Exception as exc:
            logger.warning("DB health check failed for %s: %s", project_id, exc, exc_info=True)
            return "error"

    async def update_project(self, project_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        json_updates = {k: v for k, v in updates.items()
                        if k in ("name", "description") and v is not None}
        config_updates = {k: v for k, v in updates.items()
                          if k in ("main_file", "engine") and v is not None}

        def _mutate(projects):
            entry = projects.get(project_id)
            if not isinstance(entry, dict) or self._status_of(entry) != PROJECT_STATUS_ACTIVE:
                raise ProjectNotFoundError(project_id)
            if json_updates:
                entry.update(json_updates)
            if json_updates or config_updates:
                entry["modified"] = to_iso(utcnow())

        self._update_projects(_mutate)

        if config_updates:
            await self._set_config(project_id, config_updates)

        return await self.get_project(project_id)

    # ------------------------------------------------------------------
    # Delete project — helpers + orchestration
    # ------------------------------------------------------------------

    async def _collect_running_doc_ids_async(self, project_id: str) -> list:
        """Collect IDs of documents still being processed."""
        running = []
        try:
            from app.database.unit_of_work import UnitOfWork
            async with UnitOfWork(project_id, allow_inactive=True) as uow:
                all_docs = await uow.library.get_all()
            for doc in all_docs:
                if not doc.is_folder and doc.processing_status in ACTIVE_STATUSES:
                    running.append(doc.id)
        except Exception as exc:
            logger.warning(
                "Failed to collect running doc IDs for project %s: %s",
                project_id,
                exc,
                exc_info=True,
            )
        return running

    async def _cancel_library_tasks(self, project_id: str, doc_ids: list) -> None:
        """Cancel queued/running durable library background tasks."""
        try:
            from app.services.background_task_service import background_task_service
        except ImportError:
            logger.warning("background_task_service not available, skipping task cancellation")
            return
        try:
            await background_task_service.cancel_project_tasks(project_id)
        except Exception as exc:
            logger.warning(
                "Failed to cancel project background tasks for %s: %s",
                project_id,
                exc,
                exc_info=True,
            )
        for doc_id in doc_ids:
            try:
                await background_task_service.cancel_document_tasks(project_id, doc_id)
            except Exception as exc:
                logger.warning("Failed to cancel task for doc %s: %s", doc_id, exc, exc_info=True)

    async def _evict_project_caches(self, project_id: str, db_manager) -> None:
        """Evict in-memory caches (RAG, DB engine)."""
        try:
            from app.services.rag_service import rag_service
            rag_service.evict_project(project_id)
        except Exception as exc:
            logger.warning("Failed to evict RAG cache for project %s: %s", project_id, exc, exc_info=True)
        await db_manager.cleanup_project(project_id)

    def _delete_project_directory(self, project_id: str) -> None:
        """Delete the project directory from disk."""
        p_path = self.USERDATA_DIR / project_id
        try:
            shutil.rmtree(p_path, ignore_errors=True)
        except Exception as exc:
            logger.warning("Failed to delete directory for project %s: %s", project_id, exc, exc_info=True)

    async def _kill_project_kernels(self, project_id: str) -> None:
        """Kill Jupyter kernels belonging to a project."""
        try:
            from app.services.jupyter_service import get_jupyter
            jupyter_svc = get_jupyter()
            if jupyter_svc:
                await jupyter_svc.kill_project_kernels(project_id)
        except Exception as exc:
            logger.warning("Failed to kill Jupyter kernels for project %s: %s", project_id, exc, exc_info=True)

    async def _cleanup_worker_state(self, project_id: str) -> None:
        """Cancel queued Huey tasks and active stream sessions for a project.

        Best-effort: each step logs and swallows its own failure so the
        surrounding project cleanup (deletion or reset) continues. Shared
        by ``delete_project`` and ``reset_database`` to keep their
        worker-cleanup contract identical.
        """
        try:
            from app.workers.huey_tasks import purge_project_tasks
            purge_project_tasks(project_id)
        except Exception as exc:
            logger.warning(
                "Failed to purge Huey tasks for project %s: %s",
                project_id, exc, exc_info=True,
            )
        try:
            from app.workers.stream_server import stream_server
            await stream_server.cancel_project(project_id)
        except Exception as exc:
            logger.warning(
                "Failed to cancel streams for project %s: %s",
                project_id, exc, exc_info=True,
            )

    async def delete_project(self, project_id: str):
        """Delete a project and all associated data.

        Order matters:
        1. Mark deleting in the global registry (cross-process barrier)
        2. Collect running doc IDs through the internal inactive-project path
        3. Cancel durable library tasks while project DB is still accessible
        4. Mark the DB manager deleted so no new sessions can open
        5. Cancel queued Huey tasks and active stream sessions
        6. Evict in-memory caches
        7. Delete project directory
        8. Mark deleted and kill Jupyter kernels
        """
        from app.database.manager import get_db_manager

        self.mark_project_deleting(project_id)

        running_doc_ids = await self._collect_running_doc_ids_async(project_id)

        await self._cancel_library_tasks(project_id, running_doc_ids)

        db_manager = await get_db_manager()
        db_manager.mark_deleted(project_id)

        await self._cleanup_worker_state(project_id)

        await self._evict_project_caches(project_id, db_manager)
        self._delete_project_directory(project_id)
        self.mark_project_deleted(project_id)
        await self._kill_project_kernels(project_id)

    async def reset_database(self, project_id: str) -> None:
        """Delete the project's SQLite database so it is recreated fresh on next access.

        Project files (documents, images, code) are untouched — only the DB
        (chat history, annotations, tasks, library metadata) is removed.
        """
        from app.database.manager import get_db_manager

        self.mark_project_resetting(project_id)
        try:
            running_doc_ids = await self._collect_running_doc_ids_async(project_id)
            await self._cancel_library_tasks(project_id, running_doc_ids)
            await self._cleanup_worker_state(project_id)

            db_manager = await get_db_manager()
            await self._evict_project_caches(project_id, db_manager)
            await db_manager.reset_project_database(project_id)
            logger.info("Reset database for project %s", project_id)
        finally:
            self.mark_project_active(project_id)


# Singleton
project_service = ProjectService()
