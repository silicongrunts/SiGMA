"""Verify the single post-initial migration and its SQLite behavior."""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.database.models import Base


BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_DIR = BACKEND_DIR / "alembic"
INITIAL_REVISION = "bb1b30aeff77"


def _alembic_config(db_path: Path) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return config


def _upgrade_to(db_path: Path, revision: str) -> None:
    command.upgrade(_alembic_config(db_path), revision)


def _downgrade_to(db_path: Path, revision: str) -> None:
    command.downgrade(_alembic_config(db_path), revision)


def _build_fresh_db(db_path: Path) -> None:
    _upgrade_to(db_path, "head")


def _sqlite_names(conn: sqlite3.Connection, object_type: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", (object_type,)
        )
    }


def _insert_task_state(
    conn: sqlite3.Connection,
    task_id: str,
    owner_id: str,
    status: str,
) -> None:
    conn.execute(
        "INSERT INTO task_state "
        "(task_id, owner_type, owner_id, status, task_type, created_at, updated_at) "
        "VALUES (?, 'chat_session', ?, ?, 'llm', '', '')",
        (task_id, owner_id, status),
    )


def _insert_library_document(
    conn: sqlite3.Connection, doc_id: str, body: str
) -> None:
    conn.execute(
        "INSERT INTO library_documents "
        "(id, title, description, content, doc_type, revision, "
        "processing_status, processing_log, is_folder, created_at, updated_at) "
        "VALUES (?, ?, '', ?, 'text', 1, 'completed', '', 0, "
        "'2024-01-01', '2024-01-01')",
        (doc_id, doc_id, body),
    )


def _fts_search(conn: sqlite3.Connection, term: str) -> list[tuple]:
    """Search the way the application does: join FTS rowids back to rows."""
    return conn.execute(
        "SELECT d.id FROM library_documents_fts f "
        "JOIN library_documents d ON d.rowid = f.rowid "
        "WHERE library_documents_fts MATCH ?",
        (term,),
    ).fetchall()


def test_migrations_have_one_head_after_initial() -> None:
    scripts = ScriptDirectory(str(ALEMBIC_DIR))
    assert scripts.get_heads() == ["c8a6d03117f9"]
    assert scripts.get_revision("c8a6d03117f9").down_revision == INITIAL_REVISION


def test_migrated_schema_matches_models() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "schema.db"
        _build_fresh_db(db_path)
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as connection:
                context = MigrationContext.configure(
                    connection,
                    opts={"compare_type": True},
                )
                diffs = compare_metadata(context, Base.metadata)
        finally:
            engine.dispose()

    relevant_diffs = [
        diff
        for diff in diffs
        if not (
            diff
            and diff[0] == "remove_table"
            and (
                str(diff[1].name).endswith("_fts")
                or "_fts_" in str(diff[1].name)
                or diff[1].name == "alembic_version"
            )
        )
    ]
    assert not relevant_diffs, "schema drift: " + repr(relevant_diffs)


def test_initial_to_head_backfills_completed_library_row() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "initial-to-head.db"
        _upgrade_to(db_path, INITIAL_REVISION)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO library_documents "
                "(id, title, description, content, doc_type, revision, "
                "processing_status, processing_log, is_folder, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy",
                    "legacy",
                    "",
                    "old body",
                    "txt",
                    7,
                    "completed",
                    "",
                    0,
                    "2024-01-01",
                    "2024-01-01",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        _upgrade_to(db_path, "head")
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT indexed_revision, indexed_generation, index_generation "
                "FROM library_documents WHERE id = 'legacy'"
            ).fetchone()
        finally:
            conn.close()

    assert row == (7, 0, 0)


def test_initial_to_head_drops_columns_and_preserves_rows() -> None:
    """bb1b30 → head rebuilds ``sessions`` via batch mode. The rebuild is a
    ``DROP TABLE``, whose implicit DELETE fires ``ON DELETE CASCADE`` into
    messages/tasks under foreign-key enforcement — this test fails if that
    ever wipes user data instead of only dropping the redundant columns."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "issue-63.db"
        _upgrade_to(db_path, INITIAL_REVISION)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO sessions (id, project_id, title, session_kind, "
                "created_at, updated_at, is_archived) "
                "VALUES ('s1', 'legacy-pid', 'Chat', 'chat', "
                "'2024-01-01', '2024-01-01', 0)"
            )
            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, "
                "token_count, cached_tokens, input_tokens, is_boundary, seq, "
                "created_at) VALUES "
                "('m1', 's1', 'user', 'hello', 0, 0, 0, 0, 0, '2024-01-01')"
            )
            conn.execute(
                "INSERT INTO tasks (id, session_id, subject, description, "
                "status, seq, created_at, updated_at) VALUES "
                "('t1', 's1', 'Subject', 'Desc', 'queued', 0, "
                "'2024-01-01', '2024-01-01')"
            )
            conn.execute(
                "INSERT INTO background_tasks (id, project_id, kind, queue, "
                "status, priority, payload_json, attempt_count, max_attempts, "
                "created_at, updated_at) VALUES "
                "('b1', 'legacy-pid', 'document_process', 'library', 'queued', "
                "100, '{}', 0, 3, '2024-01-01', '2024-01-01')"
            )
            conn.commit()
        finally:
            conn.close()

        _upgrade_to(db_path, "head")
        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM sessions"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = 's1'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE session_id = 's1'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM background_tasks WHERE id = 'b1'"
            ).fetchone()[0] == 1

            assert "project_id" not in {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)")
            }
            assert "project_id" not in {
                row[1] for row in conn.execute("PRAGMA table_info(background_tasks)")
            }
            assert "embedding_id" not in {
                row[1] for row in conn.execute("PRAGMA table_info(library_documents)")
            }
            assert "ix_sessions_project_id" not in _sqlite_names(conn, "index")
            assert "ix_background_tasks_project_id" not in _sqlite_names(conn, "index")
        finally:
            conn.close()


def test_initial_to_head_collapses_legacy_owner_conflicts() -> None:
    """The owner-uniqueness indexes close a submission race the v0.1.x app
    guarded only at the application layer, so a legacy database may already
    hold duplicate runnable (or parked) rows for one owner. The migration
    must collapse them to the newest row per status set instead of failing
    CREATE UNIQUE INDEX at startup, which would brick the project."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "owner-conflicts.db"
        _upgrade_to(db_path, INITIAL_REVISION)
        conn = sqlite3.connect(str(db_path))
        try:
            # Insertion order (== rowid order): stale rows first.
            _insert_task_state(conn, "stale-running", "o1", "running")
            _insert_task_state(conn, "fresh-queued", "o1", "queued")
            _insert_task_state(conn, "old-awaiting", "o2", "awaiting_input")
            _insert_task_state(conn, "newer-awaiting", "o2", "awaiting_input")
            # Parked + runnable coexistence for one owner is legal and
            # must survive the dedupe untouched.
            _insert_task_state(conn, "o1-parked", "o1", "awaiting_input")
            conn.commit()
        finally:
            conn.close()

        _upgrade_to(db_path, "head")

        conn = sqlite3.connect(str(db_path))
        try:
            rows = {
                task_id: (owner_id, status)
                for task_id, owner_id, status in conn.execute(
                    "SELECT task_id, owner_id, status FROM task_state"
                )
            }
            assert rows == {
                "fresh-queued": ("o1", "queued"),
                "o1-parked": ("o1", "awaiting_input"),
                "newer-awaiting": ("o2", "awaiting_input"),
            }
        finally:
            conn.close()


def test_initial_to_head_rebuilds_fts_index_after_rowid_renumber() -> None:
    """bb1b30 → head rebuilds ``library_documents`` three times via batch
    mode, which assigns fresh sequential rowids. On a legacy database with
    rowid gaps from deleted documents, the external-content FTS index keeps
    pointing at stale rowids — keyword search would silently return the
    wrong document or miss rows. The migration must rebuild the index from
    the content table."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fts-rebuild.db"
        _upgrade_to(db_path, INITIAL_REVISION)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_library_document(conn, "docA", "xxalphauniquetokenxx")
            _insert_library_document(conn, "docB", "xxbetauniquetokenxx")
            _insert_library_document(conn, "docC", "xxgammauniquetokenxx")
            conn.commit()
            # Triggers from the initial migration index the documents; the
            # delete leaves a rowid gap for the rebuild to compact.
            assert _fts_search(conn, "betauniquetoken") == [("docB",)]
            conn.execute("DELETE FROM library_documents WHERE id = 'docA'")
            conn.commit()
        finally:
            conn.close()

        _upgrade_to(db_path, "head")

        conn = sqlite3.connect(str(db_path))
        try:
            assert _fts_search(conn, "betauniquetoken") == [("docB",)]
            assert _fts_search(conn, "gammauniquetoken") == [("docC",)]
            assert _fts_search(conn, "alphauniquetoken") == []

            # Post-migration writes through the recreated triggers must not
            # leave stale or ghost entries behind.
            conn.execute(
                "UPDATE library_documents SET content = 'xxdeltauniquetokenxx' "
                "WHERE id = 'docB'"
            )
            conn.execute("DELETE FROM library_documents WHERE id = 'docC'")
            conn.commit()
            assert _fts_search(conn, "deltauniquetoken") == [("docB",)]
            assert _fts_search(conn, "betauniquetoken") == []
            assert _fts_search(conn, "gammauniquetoken") == []
        finally:
            conn.close()


def test_head_downgrade_to_initial_and_upgrade_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "roundtrip.db"
        _build_fresh_db(db_path)
        _downgrade_to(db_path, INITIAL_REVISION)

        conn = sqlite3.connect(str(db_path))
        try:
            assert "heartbeat_at" in {
                row[1] for row in conn.execute("PRAGMA table_info(task_state)")
            }
            assert "not_before" not in {
                row[1] for row in conn.execute("PRAGMA table_info(background_tasks)")
            }
            assert not {
                "indexed_revision",
                "indexed_generation",
                "index_generation",
            } & {
                row[1] for row in conn.execute("PRAGMA table_info(library_documents)")
            }
            assert "annotation_file_states" not in _sqlite_names(conn, "table")
            assert "annotation_file_transactions" not in _sqlite_names(conn, "table")
            assert "project_id" in {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)")
            }
            assert "project_id" in {
                row[1] for row in conn.execute("PRAGMA table_info(background_tasks)")
            }
            assert "embedding_id" in {
                row[1] for row in conn.execute("PRAGMA table_info(library_documents)")
            }
            assert "ix_sessions_project_id" in _sqlite_names(conn, "index")
            assert "ix_background_tasks_project_id" in _sqlite_names(conn, "index")
        finally:
            conn.close()

        _upgrade_to(db_path, "head")
        conn = sqlite3.connect(str(db_path))
        try:
            assert "not_before" in {
                row[1] for row in conn.execute("PRAGMA table_info(background_tasks)")
            }
            assert "project_id" not in {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)")
            }
            assert "project_id" not in {
                row[1] for row in conn.execute("PRAGMA table_info(background_tasks)")
            }
            assert "annotation_file_states" in _sqlite_names(conn, "table")
            assert "annotation_file_transactions" in _sqlite_names(conn, "table")
        finally:
            conn.close()


def test_fts_triggers_exist_and_sync_documents() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fts.db"
        _build_fresh_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            assert {
                "library_documents_ai",
                "library_documents_ad",
                "library_documents_au",
            } <= _sqlite_names(conn, "trigger")

            conn.execute(
                "INSERT INTO library_documents "
                "(id, title, description, content, doc_type, revision, "
                "processing_status, processing_log, is_folder, created_at, updated_at) "
                "VALUES ('d1', 'quantum', '', 'entanglement', 'text', 1, "
                "'completed', '', 0, '2024-01-01', '2024-01-01')"
            )
            assert conn.execute(
                "SELECT 1 FROM library_documents_fts "
                "WHERE library_documents_fts MATCH 'uantum'"
            ).fetchone()

            conn.execute(
                "UPDATE library_documents SET title = 'classical' WHERE id = 'd1'"
            )
            assert conn.execute(
                "SELECT 1 FROM library_documents_fts "
                "WHERE library_documents_fts MATCH 'lassical'"
            ).fetchone()
            assert conn.execute(
                "SELECT 1 FROM library_documents_fts "
                "WHERE library_documents_fts MATCH 'uantum'"
            ).fetchone() is None

            conn.execute("DELETE FROM library_documents WHERE id = 'd1'")
            assert conn.execute(
                "SELECT 1 FROM library_documents_fts "
                "WHERE library_documents_fts MATCH 'lassical'"
            ).fetchone() is None
        finally:
            conn.close()


def test_task_state_partial_unique_indexes_enforce_status_sets() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "task-state.db"
        _build_fresh_db(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            indexes = {
                row[1]: row[4]
                for row in conn.execute("PRAGMA index_list(task_state)")
            }
            assert indexes["uq_task_state_owner_runnable"] == 1
            assert indexes["uq_task_state_owner_parked"] == 1

            _insert_task_state(conn, "queued", "owner", "queued")
            with pytest.raises(sqlite3.IntegrityError):
                _insert_task_state(conn, "running", "owner", "running")
            conn.rollback()

            for status, owner_id in (
                ("awaiting_input", "awaiting"),
                ("interaction_consuming", "consuming"),
                ("interaction_failed", "failed"),
            ):
                _insert_task_state(conn, f"{status}-1", owner_id, status)
                with pytest.raises(sqlite3.IntegrityError):
                    _insert_task_state(conn, f"{status}-2", owner_id, status)
                conn.rollback()

            _insert_task_state(conn, "parked", "owner", "awaiting_input")
            conn.commit()
        finally:
            conn.close()


def test_current_columns_indexes_and_tables_are_present() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "objects.db"
        _build_fresh_db(db_path)
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            database_inspector = inspect(engine)
            assert {
                "indexed_revision",
                "indexed_generation",
                "index_generation",
            } <= {
                column["name"]
                for column in database_inspector.get_columns("library_documents")
            }
            assert "not_before" in {
                column["name"]
                for column in database_inspector.get_columns("background_tasks")
            }
            assert "heartbeat_at" not in {
                column["name"]
                for column in database_inspector.get_columns("task_state")
            }
            assert "project_id" not in {
                column["name"]
                for column in database_inspector.get_columns("sessions")
            }
            assert "project_id" not in {
                column["name"]
                for column in database_inspector.get_columns("background_tasks")
            }
            assert "embedding_id" not in {
                column["name"]
                for column in database_inspector.get_columns("library_documents")
            }
            session_indexes = {
                index["name"]
                for index in database_inspector.get_indexes("sessions")
            }
            background_indexes = {
                index["name"]
                for index in database_inspector.get_indexes("background_tasks")
            }
            assert "ix_sessions_project_id" not in session_indexes
            assert "ix_background_tasks_project_id" not in background_indexes
            assert database_inspector.get_indexes("annotation_file_transactions") == [
                {
                    "name": "ix_annotation_file_transactions_file_path",
                    "column_names": ["file_path"],
                    "unique": 0,
                    "dialect_options": {},
                }
            ]
        finally:
            engine.dispose()
