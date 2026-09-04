"""
Shared test fixtures for SiGMA backend tests.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import app.core.config as config_module
from app.core import project_registry
from app.database.manager import get_db_manager


BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = BACKEND_DIR / "alembic"

# Default marker for each test subdirectory, applied by
# ``pytest_collection_modifyitems`` below to files that carry no explicit
# marker.  A file opts out by declaring its own ``pytestmark`` or per-test
# markers.  Keep this mapping in sync with RULES/TESTING.md.
DEFAULT_MARKER_BY_DIR = {
    "agents": "unit",
    "core": "unit",
    "files": "unit",
    "library": "unit",
    "ai": "integration",
    "projects": "integration",
    "services": "integration",
    "synthesis": "integration",
    "scripts": "integration",
    "routes": "route",
    "database": "database",
    "architecture": "architecture",
}


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.own_markers:
            continue
        try:
            parts = Path(item.fspath).relative_to(Path(__file__).parent).parts
        except ValueError:
            continue
        for part in parts[:-1]:
            marker_name = DEFAULT_MARKER_BY_DIR.get(part)
            if marker_name:
                item.add_marker(getattr(pytest.mark, marker_name))
                break


@pytest_asyncio.fixture
async def db_engine():
    """Create a fresh SQLite engine whose schema is built by Alembic.

    Using ``alembic upgrade head`` (not ``Base.metadata.create_all``) keeps
    the test path identical to the production migration path.  Any drift
    between models and migrations surfaces as a test failure, not just as a
    broken user upgrade.

    The upgrade runs in a worker thread because env.py calls
    ``asyncio.run()`` internally, which cannot be nested inside the test's
    already-running event loop.

    ``connect_args={"timeout": 30}`` passes the busy_timeout directly
    to ``sqlite3.connect()`` — this is the reliable way to set it with
    aiosqlite, unlike PRAGMA which may not fire on the background thread.
    """
    from alembic.config import Config
    from alembic import command

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        alembic_ini = BACKEND_DIR / "alembic.ini"
        cfg = Config(str(alembic_ini))
        cfg.set_main_option("script_location", str(ALEMBIC_DIR))
        cfg.set_main_option(
            "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}",
        )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, command.upgrade, cfg, "head")

        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"timeout": 30},
        )

        # Mirror production DatabaseManager: FK enforcement is on for every
        # connection (SQLite defaults it off, silently no-op'ing CASCADE).
        # Tests that insert child rows must seed their parents, exactly as
        # they would against the real database.
        @sa_event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        yield engine
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    """Return an async session factory bound to the test engine."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def project(tmp_path, monkeypatch):
    """A real per-project database under a temporary userdata root."""
    root = tmp_path / "userdata"
    (root / ".SiGMA").mkdir(parents=True)
    pid = f"proj{uuid4().hex[:20]}"
    (root / pid / ".SiGMA").mkdir(parents=True)
    (root / ".SiGMA" / "projects.json").write_text(json.dumps({
        pid: {"status": "active", "name": "Project", "description": ""},
    }))
    monkeypatch.setattr(config_module, "USERDATA_DIR", root)
    monkeypatch.setattr(project_registry, "_registry_cache", None)
    monkeypatch.setattr(project_registry, "_registry_mtime", 0.0)

    manager = await get_db_manager()
    await manager.ensure_db_exists(pid)
    yield pid
    await manager.cleanup_project(pid)
