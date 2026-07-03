"""
Alembic environment for SiGMA per-project SQLite databases.

Both new and existing databases are brought to head via ``alembic upgrade
head`` — there is no separate ``Base.metadata.create_all`` path.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.database.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL generation)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _include_object(object_, name, type_, reflected, compare_to):
    """Exclude FTS5 virtual tables, shadow tables, and triggers from autogenerate.

    Without this, ``alembic revision --autogenerate`` would try to DROP
    ``library_documents_fts`` and its triggers because they exist in the DB
    but not in ``Base.metadata``.  FTS5 also creates internal shadow tables
    (``_data``, ``_idx``, ``_docsize``, ``_config``) that must be excluded.
    """
    if type_ == "table" and (name.endswith("_fts") or "_fts_" in name):
        return False
    if type_ == "trigger":
        return False
    return True


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # Required for batch_alter_table with SQLite
        compare_type=True,              # Detect column type changes
        include_object=_include_object,  # Exclude FTS5 tables/triggers
        # compare_server_default is intentionally omitted: SQLite represents
        # TEXT server defaults as TextClause while the model uses literal
        # strings, producing false-positive diffs on autogenerate.
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    from sqlalchemy import event as sa_event

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    @sa_event.listens_for(connectable.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
