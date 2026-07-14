"""Low-level read-only access to the project registry.

The project registry (``USERDATA_DIR/.SiGMA/projects.json``) maps project
IDs to metadata entries.  Several lower layers need to consult a project's
status without depending on ``services.project_service``:

* ``database.manager`` gates per-project DB initialization on status.
* ``workers.huey_tasks`` skips queued work whose project is no longer active.

Routing those reads through ``project_service`` would create a
``database -> services`` (and ``workers -> services``) edge that violates
the layer model.  Keeping the read here preserves a clean DAG.

Write access, status transitions, and corrupt-file recovery remain in
``services.project_service``; this module is intentionally read-only and
best-effort.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from app.core.atomic_file import ProjectFileLock, safe_read_json
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

PROJECT_STATUS_ACTIVE = "active"

# In-process cache for the project registry. ``_read_registry`` is on the hot
# path of both the web event loop (``GET /projects``) and the worker heartbeat
# loop (``is_project_active``). ``ProjectFileLock`` uses a blocking
# ``fcntl.flock(LOCK_EX)``; when the web and worker processes contend on it,
# the synchronous syscall freezes the async event loop and stalls every HTTP
# request. Caching keyed on the file's mtime lets the common case (registry
# unchanged) return instantly without touching the lock. Writes go through
# ``project_service._update_projects`` which uses ``os.replace`` — that always
# bumps the mtime, so the cache invalidates automatically on the next read.
_registry_cache: dict | None = None
_registry_mtime: float = 0.0


def _projects_file() -> Path:
    """Absolute path to the project registry file."""
    return settings.USERDATA_DIR / ".SiGMA" / "projects.json"


def _read_registry() -> dict:
    """Best-effort read of the project registry.

    Returns an empty dict when the file is missing or unparsable.  Lower
    layers (DB init, worker gating) treat an unreadable registry as
    "no projects eligible" rather than crashing; the service layer owns
    any backup-and-rebuild recovery policy.
    """
    global _registry_cache, _registry_mtime

    path = _projects_file()

    # Fast path — ``os.stat`` is non-blocking and never contends on the flock.
    # If the registry has not changed since the last locked read, reuse the
    # cached value and skip the lock entirely.
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        _registry_cache = None
        _registry_mtime = 0.0
        return {}
    if _registry_cache is not None and mtime == _registry_mtime:
        return _registry_cache

    try:
        with ProjectFileLock(path):
            data = safe_read_json(path)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.warning("Project registry at %s is unreadable: %s", path, exc)
        return {}

    _registry_cache = data
    _registry_mtime = mtime
    return data


def get_project_status(project_id: str) -> Optional[str]:
    """Return the status string recorded for *project_id*.

    Returns ``None`` when the project is not registered.  An entry that
    omits an explicit ``status`` field is treated as active, matching
    ``project_service``'s public contract.
    """
    entry = _read_registry().get(project_id)
    if not isinstance(entry, dict):
        return None
    return entry.get("status") or PROJECT_STATUS_ACTIVE


def is_project_active(project_id: str) -> bool:
    """Return True iff *project_id* is registered as active and on disk."""
    if get_project_status(project_id) != PROJECT_STATUS_ACTIVE:
        return False
    return (settings.USERDATA_DIR / project_id).is_dir()
