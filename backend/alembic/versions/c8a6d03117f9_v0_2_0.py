"""v0.2.0 schema: annotation CAS journal, RAG index tracking, task
ownership uniqueness; drop redundant columns."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8a6d03117f9"
down_revision: Union[str, None] = "bb1b30aeff77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def _restore_library_fts_triggers() -> None:
    for trigger in _FTS_TRIGGERS:
        op.execute(trigger)


def _rebuild_library_fts_index() -> None:
    # Batch rebuilds copy rows via ``INSERT INTO ... SELECT``, which assigns
    # fresh sequential rowids — any rowid gaps from past deletes disappear and
    # the external-content FTS index desyncs from the content table (searches
    # return the wrong row or ghost rowids). Rebuilding the index from the
    # content table restores the mapping; a no-op on fresh/empty databases.
    op.execute(
        "INSERT INTO library_documents_fts(library_documents_fts) "
        "VALUES('rebuild')"
    )


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


def upgrade() -> None:
    with op.batch_alter_table("task_state") as batch_op:
        batch_op.drop_column("heartbeat_at")

    _dedupe_legacy_owner_conflicts()
    op.create_index(
        "uq_task_state_owner_runnable",
        "task_state",
        ["owner_type", "owner_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running', 'cancelling')"),
    )
    op.create_index(
        "uq_task_state_owner_parked",
        "task_state",
        ["owner_type", "owner_id"],
        unique=True,
        sqlite_where=sa.text(
            "status IN ('awaiting_input', 'interaction_consuming', "
            "'interaction_failed')"
        ),
    )

    with op.batch_alter_table("background_tasks") as batch_op:
        batch_op.add_column(sa.Column("not_before", sa.DateTime(), nullable=True))

    with op.batch_alter_table("library_documents") as batch_op:
        batch_op.add_column(sa.Column("indexed_revision", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("indexed_generation", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("index_generation", sa.Integer(), nullable=True)
        )
    _update_library_index_metadata()
    with op.batch_alter_table("library_documents") as batch_op:
        batch_op.alter_column(
            "index_generation",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="0",
        )
    _restore_library_fts_triggers()

    op.create_table(
        "annotation_file_states",
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_hash", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("file_path", name="pk_annotation_file_states"),
    )
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
    op.create_index(
        "ix_annotation_file_transactions_file_path",
        "annotation_file_transactions",
        ["file_path"],
    )

    # ── issue #63: drop redundant columns ──
    # The per-project DB file already encodes the project id, so the
    # ``project_id`` columns (and their indexes) on ``sessions`` and
    # ``background_tasks`` are dead weight, and
    # ``library_documents.embedding_id`` was never used.
    #
    # Batch mode rebuilds tables via ``DROP TABLE``, whose implicit DELETE
    # fires ``ON DELETE CASCADE`` into messages/tasks while foreign key
    # enforcement is on — dropping the sessions table would wipe the chat
    # history. The pragma can only change outside a transaction, hence the
    # autocommit blocks around it.
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_index("ix_sessions_project_id")
        batch_op.drop_column("project_id")
    with op.batch_alter_table("background_tasks") as batch_op:
        batch_op.drop_index("ix_background_tasks_project_id")
        batch_op.drop_column("project_id")
    with op.batch_alter_table("library_documents") as batch_op:
        batch_op.drop_column("embedding_id")
    _restore_library_fts_triggers()
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=ON")

    # Last step: the table rebuilds above renumbered rowids, so the FTS
    # index must be rebuilt from the content table to stay consistent.
    _rebuild_library_fts_index()


def downgrade() -> None:
    op.drop_index(
        "ix_annotation_file_transactions_file_path",
        table_name="annotation_file_transactions",
    )
    op.drop_table("annotation_file_transactions")
    op.drop_table("annotation_file_states")

    with op.batch_alter_table("library_documents") as batch_op:
        batch_op.drop_column("index_generation")
        batch_op.drop_column("indexed_generation")
        batch_op.drop_column("indexed_revision")
    _restore_library_fts_triggers()
    _rebuild_library_fts_index()

    with op.batch_alter_table("background_tasks") as batch_op:
        batch_op.drop_column("not_before")

    with op.batch_alter_table("task_state") as batch_op:
        batch_op.add_column(sa.Column("heartbeat_at", sa.String(), nullable=True))

    op.drop_index("uq_task_state_owner_parked", table_name="task_state")
    op.drop_index("uq_task_state_owner_runnable", table_name="task_state")

    # Restore the columns dropped above. ADD COLUMN never rebuilds the table,
    # so no foreign-key/trigger dance is needed; rows migrated out keep
    # project_id="" (the value no longer matters — nothing reads it).
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
