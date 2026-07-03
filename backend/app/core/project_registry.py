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
from pathlib import Path
from typing import Optional

from app.core.atomic_file import ProjectFileLock, safe_read_json
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

PROJECT_STATUS_ACTIVE = "active"


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
    path = _projects_file()
    try:
        with ProjectFileLock(path):
            return safe_read_json(path)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.warning("Project registry at %s is unreadable: %s", path, exc)
        return {}


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
