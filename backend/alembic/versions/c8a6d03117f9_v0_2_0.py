"""v0.2.0 schema: annotation CAS journal, RAG index tracking, task
ownership uniqueness; drop redundant columns."""

import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


revision: str = "c8a6d03117f9"
down_revision: Union[str, None] = "bb1b30aeff77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Reuse alembic's configured logger: the app surfaces its output on the
# console and in the server log without extra logging wiring here.
_logger = logging.getLogger("alembic.runtime.migration")


_FTS_TRIGGER_NAMES = (
    "library_documents_ai",
    "library_documents_ad",
    "library_documents_au",
)

_FTS_TRIGGERS = (
    """
    CREATE TRIGGER library_documents_ai AFTER INSERT ON library_documents BEGIN
        INSERT INTO library_documents_fts(rowid, title, description, content)
        VALUES (new.rowid, new.title, new.description, new.content);
    END
    """,
    """
    CREATE TRIGGER library_documents_ad AFTER DELETE ON library_documents BEGIN
        INSERT INTO library_documents_fts(
            library_documents_fts, rowid, title, description, content
        ) VALUES ('delete', old.rowid, old.title, old.description, old.content);
    END
    """,
    """
    CREATE TRIGGER library_documents_au AFTER UPDATE ON library_documents BEGIN
        INSERT INTO library_documents_fts(
            library_documents_fts, rowid, title, description, content
        ) VALUES ('delete', old.rowid, old.title, old.description, old.content);
        INSERT INTO library_documents_fts(rowid, title, description, content)
        VALUES (new.rowid, new.title, new.description, new.content);
    END
    """,
)


def _drop_library_fts_triggers() -> None:
    for name in _FTS_TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")


def _restore_library_fts_triggers() -> None:
    # Drop-first keeps a retry after a failed restore from raising
    # "trigger already exists".
    _drop_library_fts_triggers()
    for trigger in _FTS_TRIGGERS:
        op.execute(trigger)


def _rebuild_library_fts_index() -> None:
    # Derive the entire index from the content table. This is the only
    # reliable repair for index drift: FTS5's 'integrity-check' command
    # and docsize comparisons have both been observed to report "ok" on
    # a drifted index that still raises SQLITE_CORRUPT ("database disk
    # image is malformed") when a sync trigger deletes from it. It also
    # restores the rowid mapping after the batch table copies below
    # renumber rowids.
    op.execute(
        "INSERT INTO library_documents_fts(library_documents_fts) "
        "VALUES('rebuild')"
    )


def _table_exists(name: str) -> bool:
    return sa_inspect(op.get_bind()).has_table(name)


def _columns_of(table: str) -> set[str]:
    return {
        column["name"]
        for column in sa_inspect(op.get_bind()).get_columns(table)
    }


def _index_exists(name: str) -> bool:
    return (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        )
        .first()
        is not None
    )


def _drop_alembic_tmp_tables() -> None:
    # A previously failed or killed upgrade leaves batch-mode temp tables
    # behind — the leading CREATE TABLE of a batch commits even when the
    # migration later rolls back — and they block every retry with
    # "table _alembic_tmp_* already exists". GLOB keeps "_" literal.
    bind = op.get_bind()
    leftovers = bind.exec_driver_sql(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name GLOB '_alembic_tmp_*'"
    ).fetchall()
    for (name,) in leftovers:
        # A leftover is garbage only while its base table still exists;
        # a batch swap interrupted between its DROP and its RENAME leaves
        # the temp table holding the only copy of the data, which must be
        # renamed back into place rather than dropped.
        base = name[len("_alembic_tmp_"):]
        if bind.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (base,),
        ).first():
            bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{name}"')
        else:
            bind.exec_driver_sql(f'ALTER TABLE "{name}" RENAME TO "{base}"')


def _update_library_index_metadata() -> None:
    op.execute(
        sa.text(
            "UPDATE library_documents SET index_generation = 0 "
            "WHERE index_generation IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE library_documents "
            "SET indexed_revision = revision, indexed_generation = 0 "
            "WHERE processing_status = 'completed' AND indexed_revision IS NULL"
        )
    )


def _dedupe_legacy_owner_conflicts() -> None:
    # The owner-uniqueness indexes close a submission race that the v0.1.x
    # app guarded only at the application layer, so a database migrated from
    # v0.1.x may already hold two runnable rows for one owner — CREATE
    # UNIQUE INDEX would then fail at startup and leave the project
    # unopenable, with "delete the database" as the only recovery advice.
    # Collapse each index-enforced status set to its newest row per owner.
    # NULL owner_id is exempt: unique indexes treat NULLs as distinct.
    status_sets = (
        "('queued', 'running', 'cancelling')",
        "('awaiting_input', 'interaction_consuming', 'interaction_failed')",
    )
    for statuses in status_sets:
        op.execute(
            sa.text(
                "DELETE FROM task_state "
                "WHERE status IN " + statuses + " "
                "AND owner_id IS NOT NULL "
                "AND rowid NOT IN ("
                "SELECT MAX(rowid) FROM task_state "
                "WHERE status IN " + statuses + " "
                "AND owner_id IS NOT NULL "
                "GROUP BY owner_type, owner_id)"
            )
        )


def _upgrade_schema() -> None:
    # Every batch rebuild below swaps tables via ``DROP TABLE``, whose
    # implicit DELETE fires ``ON DELETE CASCADE`` while foreign key
    # enforcement is on: dropping ``sessions`` would wipe chat history
    # through messages/tasks, and dropping ``library_documents`` would
    # wipe every document nested under a folder through the self-
    # referential ``parent_id`` cascade. The pragma only takes effect
    # outside a transaction, hence the autocommit blocks around the whole
    # rebuild section.
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")

    if "heartbeat_at" in _columns_of("task_state"):
        with op.batch_alter_table("task_state") as batch_op:
            batch_op.drop_column("heartbeat_at")

    _dedupe_legacy_owner_conflicts()
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_state_owner_runnable "
        "ON task_state (owner_type, owner_id) "
        "WHERE status IN ('queued', 'running', 'cancelling')"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_state_owner_parked "
        "ON task_state (owner_type, owner_id) "
        "WHERE status IN ('awaiting_input', 'interaction_consuming', "
        "'interaction_failed')"
    )

    if "not_before" not in _columns_of("background_tasks"):
        with op.batch_alter_table("background_tasks") as batch_op:
            batch_op.add_column(sa.Column("not_before", sa.DateTime(), nullable=True))

    missing_index_columns = [
        (name, type_)
        for name, type_ in (
            ("indexed_revision", sa.Integer()),
            ("indexed_generation", sa.Integer()),
            ("index_generation", sa.Integer()),
        )
        if name not in _columns_of("library_documents")
    ]
    if missing_index_columns:
        with op.batch_alter_table("library_documents") as batch_op:
            for name, type_ in missing_index_columns:
                batch_op.add_column(sa.Column(name, type_, nullable=True))
    _update_library_index_metadata()
    with op.batch_alter_table("library_documents") as batch_op:
        batch_op.alter_column(
            "index_generation",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="0",
        )

    if not _table_exists("annotation_file_states"):
        op.create_table(
            "annotation_file_states",
            sa.Column("file_path", sa.String(length=500), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("file_hash", sa.String(length=64), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("file_path", name="pk_annotation_file_states"),
        )
    if not _table_exists("annotation_file_transactions"):
        op.create_table(
            "annotation_file_transactions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("file_path", sa.String(length=500), nullable=False),
            sa.Column("expected_revision", sa.Integer(), nullable=False),
            sa.Column("expected_file_hash", sa.String(length=64), nullable=False),
            sa.Column("new_file_hash", sa.String(length=64), nullable=False),
            sa.Column("old_content", sa.Text(), nullable=False),
            sa.Column("mutations", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_annotation_file_transactions"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_annotation_file_transactions_file_path "
        "ON annotation_file_transactions (file_path)"
    )

    # ── issue #63: drop redundant columns ──
    # The per-project DB file already encodes the project id, so the
    # ``project_id`` columns (and their indexes) on ``sessions`` and
    # ``background_tasks`` are dead weight, and
    # ``library_documents.embedding_id`` was never used.
    if "project_id" in _columns_of("sessions"):
        with op.batch_alter_table("sessions") as batch_op:
            if _index_exists("ix_sessions_project_id"):
                batch_op.drop_index("ix_sessions_project_id")
            batch_op.drop_column("project_id")
    if "project_id" in _columns_of("background_tasks"):
        with op.batch_alter_table("background_tasks") as batch_op:
            if _index_exists("ix_background_tasks_project_id"):
                batch_op.drop_index("ix_background_tasks_project_id")
            batch_op.drop_column("project_id")
    if "embedding_id" in _columns_of("library_documents"):
        with op.batch_alter_table("library_documents") as batch_op:
            batch_op.drop_column("embedding_id")

    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    _drop_alembic_tmp_tables()

    fts_present = _table_exists("library_documents_fts")
    if not fts_present:
        _upgrade_schema()
        return

    # Keep the sync triggers off for the whole upgrade: any statement that
    # fires them against a drifted index — the batch rebuild's DROP TABLE
    # implicit-deletes every document row — raises SQLITE_CORRUPT. The
    # leading rebuild heals drift from the v0.1.x era before the first
    # statement can trip over it.
    _drop_library_fts_triggers()
    try:
        _rebuild_library_fts_index()
        _upgrade_schema()
        # The batch table copies renumbered rowids; rederive the index.
        _rebuild_library_fts_index()
    finally:
        try:
            # Best-effort cleanup that cannot affect correctness: a failed
            # attempt must not leave the database without sync triggers, or
            # ordinary app writes drift the index further. The next attempt
            # re-heals and re-runs the guarded steps.
            _restore_library_fts_triggers()
        except Exception:
            _logger.exception(
                "Could not restore library_documents FTS triggers after the "
                "v0.2.0 migration"
            )


def downgrade() -> None:
    op.drop_index(
        "ix_annotation_file_transactions_file_path",
        table_name="annotation_file_transactions",
    )
    op.drop_table("annotation_file_transactions")
    op.drop_table("annotation_file_states")

    # Same hazard as the upgrade's library_documents rebuilds: the batch
    # DROP TABLE under foreign-key enforcement cascades through the self-
    # referential parent_id and lets the sync triggers fire, so drop the
    # triggers and switch the pragma off for the rebuild.
    _drop_library_fts_triggers()
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("library_documents") as batch_op:
        batch_op.drop_column("index_generation")
        batch_op.drop_column("indexed_generation")
        batch_op.drop_column("indexed_revision")
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=ON")
    _restore_library_fts_triggers()
    _rebuild_library_fts_index()

    with op.batch_alter_table("background_tasks") as batch_op:
        batch_op.drop_column("not_before")

    with op.batch_alter_table("task_state") as batch_op:
        batch_op.add_column(sa.Column("heartbeat_at", sa.String(), nullable=True))

    op.drop_index("uq_task_state_owner_parked", table_name="task_state")
    op.drop_index("uq_task_state_owner_runnable", table_name="task_state")

    # Restore the columns dropped above. ADD COLUMN never rebuilds the
    # table, so no foreign-key/trigger dance is needed; rows migrated out
    # keep project_id="" (the value no longer matters — nothing reads it).
    op.add_column(
        "sessions",
        sa.Column("project_id", sa.String(length=36), nullable=False,
                  server_default=""),
    )
    op.create_index("ix_sessions_project_id", "sessions", ["project_id"],
                    unique=False)
    op.add_column(
        "background_tasks",
        sa.Column("project_id", sa.String(length=36), nullable=False,
                  server_default=""),
    )
    op.create_index("ix_background_tasks_project_id", "background_tasks",
                    ["project_id"], unique=False)
    op.add_column(
        "library_documents",
        sa.Column("embedding_id", sa.String(length=100), nullable=True),
    )
