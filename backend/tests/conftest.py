"""
Shared test fixtures for SiGMA backend tests.
"""

import asyncio
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker


BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = BACKEND_DIR / "alembic"


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
        yield engine
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    """Return an async session factory bound to the test engine."""
    return async_sessionmaker(db_engine, expire_on_commit=False)
