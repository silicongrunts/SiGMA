"""
Database manager for SiGMA.

Handles database initialization and connection management.
Each project has its own SQLite database stored in .SiGMA/project_data.db
"""

import asyncio
import fcntl
import sqlite3
from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import event as sa_event

from app.core.config import settings
from app.core.exceptions import DatabaseIncompatibleError, DatabaseException
from app.core.logging import get_logger

logger = get_logger(__name__)


SQLITE_BUSY_TIMEOUT_MS = 10000


def _configure_sqlite_connection(_engine):
    """Enable FK enforcement on every new SQLite connection.

    SQLite defaults to ``foreign_keys=OFF`` — without this, ``ON DELETE
    CASCADE`` constraints silently no-op.
    """
    @sa_event.listens_for(_engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _configure_sqlite_file(db_path: Path) -> None:
    """Apply persistent SQLite pragmas before async connections open."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        conn.commit()
    finally:
        conn.close()


class DatabaseManager:
    """
    Manages database connections and initialization.

    Each project has its own SQLite database at:
    {USERDATA_DIR}/{project_id}/.SiGMA/project_data.db
    """

    def __init__(self):
        self._engines: dict[str, object] = {}       # project_id -> async engine
        self._makers: dict[str, object] = {}         # project_id -> sessionmaker
        self._initialized: set[str] = set()          # project_ids with DB ready this process
        self._deleted: set[str] = set()              # project_ids marked for deletion
        self._quarantine: set[str] = set()           # project_ids that failed migration this startup
        self._init_lock = asyncio.Lock()

    def _get_db_path(self, project_id: str) -> Path:
        """Get the database file path for a project."""
        project_path = settings.get_project_path(project_id)
        sigma_dir = project_path / ".SiGMA"
        return sigma_dir / "project_data.db"

    def _get_engine_unlocked(self, project_id: str):
        """Get or create a cached engine.

        Caller must hold ``_init_lock`` and the project must already be in
        ``_initialized``. This keeps engine creation serialized with migration
        and reset for this process.
        """
        if project_id not in self._engines:
            db_path = self._get_db_path(project_id)
            if db_path.exists():
                _configure_sqlite_file(db_path)
            db_url = f"sqlite+aiosqlite:///{db_path}"

            engine = create_async_engine(
                db_url,
                echo=False,
                pool_pre_ping=True,
                connect_args={"timeout": SQLITE_BUSY_TIMEOUT_MS / 1000},
            )
            _configure_sqlite_connection(engine)
            self._engines[project_id] = engine

            self._makers[project_id] = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )

        return self._engines[project_id]

    def get_session_maker(self, project_id: str):
        """Get a session maker for an initialized project.

        Most code should call ``get_session`` instead. This synchronous helper
        is intentionally strict so bypassing ``ensure_db_exists`` fails early.
        """
        if project_id not in self._initialized:
            raise DatabaseException(
                f"Database for project {project_id} has not been initialized"
            )
        if project_id not in self._makers:
            raise DatabaseException(
                f"Session maker for project {project_id} is not ready"
            )
        return self._makers[project_id]

    async def get_session(
        self,
        project_id: str,
        *,
        allow_inactive: bool = False,
    ) -> AsyncSession:
        """
        Get a database session for a project.

        Usage:
            async with await db_manager.get_session(project_id) as session:
                result = await session.execute(select(Message))
        """
        await self.ensure_db_exists(project_id, allow_inactive=allow_inactive)
        async with self._init_lock:
            if project_id not in self._initialized:
                raise DatabaseException(
                    f"Database for project {project_id} has not been initialized"
                )
            self._get_engine_unlocked(project_id)
            maker = self._makers[project_id]
        return maker()

    async def ensure_db_exists(self, project_id: str, *, allow_inactive: bool = False):
        """Ensure the database exists and is at the latest schema revision.

        Both new and existing databases are brought to head via
        ``alembic upgrade head`` — there is no longer a separate
        ``Base.metadata.create_all`` path.
        """
        db_path = self._get_db_path(project_id)

        if project_id in self._deleted:
            from app.core.exceptions import FileSystemError
            raise FileSystemError(f"Project {project_id} has been deleted")

        from app.core.exceptions import ProjectNotFoundError
        from app.core.project_registry import is_project_active

        if not is_project_active(project_id):
            if not allow_inactive or not db_path.exists():
                raise ProjectNotFoundError(project_id)

        # Fast path — already initialized this process lifetime
        if project_id in self._initialized:
            return

        async with self._init_lock:
            # Double-check after acquiring lock
            if project_id in self._initialized:
                return

            # is_new is checked inside the lock to avoid TOCTOU
            is_new = not db_path.exists()
            if is_new and allow_inactive:
                raise ProjectNotFoundError(project_id)

            await self._run_migrations(project_id)
            self._initialized.add(project_id)
            self._get_engine_unlocked(project_id)

    # ------------------------------------------------------------------
    # Database initialization strategies
    # ------------------------------------------------------------------

    @staticmethod
    def _lock_path_for(db_path: Path) -> Path:
        return db_path.parent / ".migrate.lock"

    @classmethod
    def _run_migration_locked(cls, cfg, db_path: Path) -> None:
        """Validate and run ``alembic upgrade head`` under a cross-process lock.

        The lock prevents the web process and the huey worker process from
        migrating the same project DB simultaneously (supervisord starts both
        in parallel).  ``fcntl.flock`` is per-machine, which matches SiGMA's
        single-container deployment.  The second caller blocks until the first
        finishes, then ``upgrade head`` is a no-op because the DB is already
        at head.
        """
        lock_path = cls._lock_path_for(db_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            cls._validate_db_revision(db_path, cfg)
            from alembic import command
            command.upgrade(cfg, "head")

    async def _run_migrations(self, project_id: str):
        """Run pending Alembic migrations on a project database.

        Works for both new databases (alembic creates the file and replays
        from the initial migration) and existing ones (only pending revisions
        are applied).  Raises RuntimeError if the DB has an incompatible
        revision (from a different SiGMA build or pre-squash).
        """
        from alembic.config import Config

        db_path = self._get_db_path(project_id)
        await self._invalidate_engine(project_id)
        _configure_sqlite_file(db_path)

        alembic_ini = Path(__file__).resolve().parent.parent.parent / "alembic.ini"
        cfg = Config(str(alembic_ini))
        cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, self._run_migration_locked, cfg, db_path,
        )

        logger.info("Migrated database for project %s", project_id)

    @staticmethod
    def _validate_db_revision(db_path: Path, cfg) -> None:
        """Validate that the DB is safe to migrate.

        Returns normally if the DB can be upgraded.  Raises RuntimeError
        with an actionable message if the DB has an incompatible revision
        — this happens when switching between SiGMA builds that have
        different migration histories (e.g. after a squash or a downgrade).
        The fix is always to delete the project database and let SiGMA
        recreate it.
        """
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(cfg)
        known = {rev.revision for rev in script.walk_revisions()}

        try:
            conn = sqlite3.connect(
                str(db_path),
                timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            )
            try:
                cursor = conn.execute(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                )
                row = cursor.fetchone()
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            # alembic_version table missing — distinguish truly empty DB
            # from a pre-alembic create_all DB that has tables but no version.
            conn = sqlite3.connect(
                str(db_path),
                timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            )
            try:
                table_count = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            finally:
                conn.close()
            if table_count > 0:
                raise DatabaseIncompatibleError(
                    f"Database {db_path.name} has tables but no alembic_version "
                    f"table — it was created by an older SiGMA build. "
                    f"Delete {db_path.name} and restart to recreate it."
                )
            return  # truly empty DB, upgrade will create everything

        if not row:
            return  # empty alembic_version — uninitialized

        current = row[0]
        if current in known:
            return  # revision is known, upgrade will apply pending migrations

        raise DatabaseIncompatibleError(
            f"Database {db_path.name} has Alembic revision '{current}' which is "
            f"not recognized by this SiGMA build. This happens after a migration "
            f"squash or when switching between incompatible versions. "
            f"Delete {db_path.name} and restart to recreate it."
        )

    @classmethod
    def _reset_database_files_locked(cls, db_path: Path) -> None:
        """Remove SQLite database files while holding the migration lock."""
        lock_path = cls._lock_path_for(db_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            if db_path.exists():
                conn = None
                try:
                    conn = sqlite3.connect(
                        str(db_path),
                        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
                    )
                    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("BEGIN EXCLUSIVE")
                    conn.rollback()
                except sqlite3.DatabaseError as exc:
                    message = str(exc).lower()
                    if "locked" in message or "busy" in message:
                        raise
                    logger.warning(
                        "Resetting unreadable SQLite database %s: %s",
                        db_path, exc,
                    )
                finally:
                    if conn is not None:
                        conn.close()
            for path in (
                db_path,
                db_path.with_name(f"{db_path.name}-wal"),
                db_path.with_name(f"{db_path.name}-shm"),
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    async def reset_project_database(self, project_id: str) -> None:
        """Dispose cached state and remove the project's SQLite database files."""
        db_path = self._get_db_path(project_id)
        async with self._init_lock:
            self._initialized.discard(project_id)
            self._quarantine.discard(project_id)
            await self._invalidate_engine(project_id)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._reset_database_files_locked, db_path)

    async def migrate_all_projects(self):
        """Migrate all existing project databases at startup.

        Scans USERDATA_DIR for project directories and runs pending
        Alembic migrations on each.  A failure for one project is logged,
        quarantined for the rest of this process lifetime, and does not
        prevent other projects from being migrated.
        """
        if not settings.USERDATA_DIR.exists():
            return

        from app.core.project_registry import is_project_active

        migrated = 0
        for project_dir in sorted(settings.USERDATA_DIR.iterdir()):
            if not project_dir.is_dir() or project_dir.name == ".SiGMA":
                continue
            pid = project_dir.name
            if pid in self._quarantine:
                continue
            if pid in self._deleted or not is_project_active(pid):
                continue
            db_path = project_dir / ".SiGMA" / "project_data.db"
            if not db_path.exists():
                continue
            async with self._init_lock:
                if pid in self._initialized:
                    continue
                try:
                    await self._run_migrations(pid)
                    self._initialized.add(pid)
                    migrated += 1
                except Exception as e:
                    self._quarantine.add(pid)
                    logger.error(
                        "Migration failed for project %s, quarantined: %s",
                        pid, e, exc_info=True,
                    )

        logger.info("Database migration complete: %d project(s) up to date", migrated)

    async def _invalidate_engine(self, project_id: str):
        """Dispose cached engine/maker for a project so it picks up schema changes."""
        if project_id in self._engines:
            old_engine = self._engines.pop(project_id)
            self._makers.pop(project_id, None)
            await old_engine.dispose()

    def mark_deleted(self, project_id: str):
        """Mark a project as deleted to prevent DB recreation."""
        self._deleted.add(project_id)

    def unmark_deleted(self, project_id: str):
        """Unmark a project as deleted (e.g. after re-creation)."""
        self._deleted.discard(project_id)

    async def cleanup_project(self, project_id: str):
        """Clean up all in-memory state for a deleted project."""
        self._initialized.discard(project_id)
        await self._invalidate_engine(project_id)

    async def cleanup_inactive_projects(self) -> list[str]:
        """Dispose cached DB state for projects that are no longer active."""
        from app.core.project_registry import is_project_active

        removed: list[str] = []
        project_ids = set(self._initialized) | set(self._engines)
        for project_id in sorted(project_ids):
            if project_id in self._deleted or not is_project_active(project_id):
                self._initialized.discard(project_id)
                await self._invalidate_engine(project_id)
                removed.append(project_id)
        return removed

    async def close_all(self):
        """Close all database connections."""
        for engine in self._engines.values():
            await engine.dispose()
        self._engines.clear()
        self._makers.clear()


# Global database manager instance
_db_manager: Optional[DatabaseManager] = None


async def get_db_manager() -> DatabaseManager:
    """Get the global database manager instance."""
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager
