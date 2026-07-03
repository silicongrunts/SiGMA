"""
Huey instance configuration for SiGMA.

Uses the vendored copy of Huey 3.0 at backend/huey/.
Stores queues and results in a single SQLite database at
userdata/.SiGMA/huey/huey.db
"""

import os
import sys

# Ensure the vendored Huey is importable before any other huey import
_VENDORED = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "huey"))
if _VENDORED not in sys.path:
    sys.path.insert(0, _VENDORED)

from huey import SqliteHuey

from app.core.config import settings


def _get_huey_db_path(sigma_dir=None):
    huey_dir = (sigma_dir or settings.SIGMA_DIR) / "huey"
    huey_dir.mkdir(parents=True, exist_ok=True)
    return huey_dir / "huey.db"


_huey_db = str(_get_huey_db_path())

huey = SqliteHuey(
    name="sigma",
    filename=_huey_db,
    results=True,           # store task results
    store_none=False,
    utc=True,
    journal_mode="wal",     # concurrency between web and consumer
)
