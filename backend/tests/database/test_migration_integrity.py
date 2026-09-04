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


def _assert_schema_matches_models(db_path: Path) -> None:
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


def test_migrations_have_one_head_after_initial() -> None:
    scripts = ScriptDirectory(str(ALEMBIC_DIR))
    assert scripts.get_heads() == ["c8a6d03117f9"]
    assert scripts.get_revision("c8a6d03117f9").down_revision == INITIAL_REVISION


def test_migrated_schema_matches_models() -> None:
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "schema.db"
        _build_fresh_db(db_path)
        _assert_schema_matches_models(db_path)


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
        conn = sqlite3.connect(str(db_path))
        try:
            # A folder-nested document must survive the downgrade's
            # library_documents rebuild despite the parent_id cascade.
            conn.executemany(
                "INSERT INTO library_documents "
                "(id, title, description, content, doc_type, revision, "
                "processing_status, processing_log, is_folder, parent_id, "
                "created_at, updated_at) "
                "VALUES (?, ?, '', ?, 'text', 1, 'completed', '', "
                "?, ?, '2024-01-01', '2024-01-01')",
                [
                    ("folder1", "folder1", "", 1, None),
                    ("docA", "docA", "xxalphatokenxx", 0, "folder1"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

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
            assert {
                row[0] for row in conn.execute("SELECT id FROM library_documents")
            } == {"folder1", "docA"}
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
            assert {
                row[0] for row in conn.execute("SELECT id FROM library_documents")
            } == {"folder1", "docA"}
            assert _fts_search(conn, "alphatoken") == [("docA",)]
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


def test_initial_to_head_heals_drifted_fts_index() -> None:
    """A v0.1.x database can carry an external-content FTS index that has
    drifted from its content table (edits or deletes made while the sync
    triggers were absent). FTS5 surfaces such drift unpredictably — from
    silently wrong search results to "database disk image is malformed"
    the moment a sync trigger fires for an affected row — which used to
    brick the upgrade at the first library_documents batch rebuild and
    strand the project.

    Drift itself cannot be reproduced deterministically (the failure is
    undefined behavior inside FTS5), so this fixture replaces the sync
    triggers with ones that abort: any statement that lets a sync trigger
    fire during the migration fails loudly and deterministically. The
    migration must complete without firing them, heal the index from the
    content table, and leave working triggers behind."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fts-drift.db"
        _upgrade_to(db_path, INITIAL_REVISION)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_library_document(conn, "docA", "xxalphatokenxx")
            _insert_library_document(conn, "docB", "xxbetatokenxx")
            conn.commit()

            # Recreate v0.1.x-era drift: edit a row while the sync
            # triggers are absent, so the index keeps the old tokens.
            for trigger in (
                "library_documents_ai",
                "library_documents_ad",
                "library_documents_au",
            ):
                conn.execute(f"DROP TRIGGER {trigger}")
            conn.execute(
                "UPDATE library_documents SET content = 'xxdeltatokenxx' "
                "WHERE id = 'docB'"
            )
            # Poison the trigger names with their real event semantics:
            # the metadata UPDATEs fire the update trigger, row deletes
            # fire the delete trigger — none of them may run during the
            # migration.
            for trigger, event in (
                ("library_documents_ai", "INSERT"),
                ("library_documents_ad", "DELETE"),
                ("library_documents_au", "UPDATE"),
            ):
                conn.execute(
                    f"CREATE TRIGGER {trigger} AFTER {event} "
                    "ON library_documents "
                    "BEGIN SELECT RAISE(ABORT, 'sync trigger fired'); END"
                )
            conn.commit()

            # Fixture proof: the poisoned triggers do fire on ordinary
            # statements against the table.
            with pytest.raises(sqlite3.DatabaseError, match="sync trigger fired"):
                conn.execute(
                    "DELETE FROM library_documents WHERE id = 'docA'"
                )
            conn.rollback()
        finally:
            conn.close()

        _upgrade_to(db_path, "head")

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0] == "c8a6d03117f9"

            # The healed index reflects current content, not the drifted
            # tokens, and passes FTS5's own consistency check.
            assert _fts_search(conn, "deltatoken") == [("docB",)]
            assert _fts_search(conn, "alphatoken") == [("docA",)]
            assert _fts_search(conn, "betatoken") == []
            conn.execute(
                "INSERT INTO library_documents_fts(library_documents_fts) "
                "VALUES('integrity-check')"
            )

            # The real sync triggers are restored and safe to fire.
            conn.execute(
                "UPDATE library_documents SET content = 'xxepsilontokenxx' "
                "WHERE id = 'docA'"
            )
            conn.commit()
            assert _fts_search(conn, "epsilontoken") == [("docA",)]
            assert _fts_search(conn, "alphatoken") == []
        finally:
            conn.close()


def test_initial_to_head_preserves_folder_nested_documents() -> None:
    """``library_documents`` carries a self-referential ``parent_id`` ON
    DELETE CASCADE. The batch rebuilds swap the table via DROP TABLE,
    whose implicit DELETE — under the foreign-key enforcement alembic's
    environment enables — cascades from every folder down through all
    nested documents, silently destroying the user's library. Rebuilds
    must run with foreign keys off; all documents must survive."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "nested-docs.db"
        _upgrade_to(db_path, INITIAL_REVISION)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executemany(
                "INSERT INTO library_documents "
                "(id, title, description, content, doc_type, revision, "
                "processing_status, processing_log, is_folder, parent_id, "
                "created_at, updated_at) "
                "VALUES (?, ?, '', ?, 'text', 1, 'completed', '', "
                "?, ?, '2024-01-01', '2024-01-01')",
                [
                    # folder → child document → grandchild folder → leaf
                    ("folder1", "folder1", "", 1, None),
                    ("docA", "docA", "xxalphatokenxx", 0, "folder1"),
                    ("subfolder", "subfolder", "", 1, "folder1"),
                    ("docB", "docB", "xxbetatokenxx", 0, "subfolder"),
                    ("docC", "docC", "xxgammatokenxx", 0, None),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        _upgrade_to(db_path, "head")

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM library_documents"
            ).fetchone()[0] == 5
            assert {
                row[0]
                for row in conn.execute("SELECT id FROM library_documents")
            } == {"folder1", "docA", "subfolder", "docB", "docC"}
            # parent/child links survive the rebuild intact.
            assert conn.execute(
                "SELECT parent_id FROM library_documents WHERE id = 'docA'"
            ).fetchone()[0] == "folder1"
            assert _fts_search(conn, "alphatoken") == [("docA",)]
            assert _fts_search(conn, "betatoken") == [("docB",)]
            assert _fts_search(conn, "gammatoken") == [("docC",)]
        finally:
            conn.close()


def test_initial_to_head_clears_alembic_tmp_leftovers() -> None:
    """A previously failed upgrade leaves the batch temp tables it created
    committed behind it (alembic executes SQLite DDL non-transactionally),
    and every retry then dies at "table _alembic_tmp_* already exists"
    before even reaching the original failure. The migration must drop
    such leftovers before doing anything else."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "tmp-leftover.db"
        _upgrade_to(db_path, INITIAL_REVISION)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_task_state(conn, "task-1", "owner-1", "queued")
            _insert_library_document(conn, "docA", "xxalphatokenxx")
            conn.commit()
            # Residue exactly as an interrupted batch rebuild leaves it.
            conn.execute(
                "CREATE TABLE _alembic_tmp_task_state "
                "(task_id VARCHAR NOT NULL, status VARCHAR NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE _alembic_tmp_library_documents "
                "(id VARCHAR NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()

        _upgrade_to(db_path, "head")

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name GLOB '_alembic_tmp_*'"
            ).fetchall() == []
            assert conn.execute(
                "SELECT COUNT(*) FROM task_state"
            ).fetchone()[0] == 1
            assert _fts_search(conn, "alphatoken") == [("docA",)]
        finally:
            conn.close()


def test_initial_to_head_survives_killed_attempt_prefix() -> None:
    """alembic executes SQLite DDL non-transactionally, so an upgrade
    killed partway (crash, power loss, SIGKILL) can leave an arbitrary
    prefix of the schema applied while alembic_version still says
    bb1b30aeff77. Re-running must treat every already-applied step as a
    no-op. This fixture applies everything except the version bump, then
    lets the migration run on top of it."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "killed-prefix.db"
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
            _insert_task_state(conn, "task-1", "owner-1", "queued")
            conn.execute(
                "INSERT INTO background_tasks (id, project_id, kind, queue, "
                "status, priority, payload_json, attempt_count, max_attempts, "
                "created_at, updated_at) VALUES "
                "('b1', 'legacy-pid', 'document_process', 'library', 'queued', "
                "100, '{}', 0, 3, '2024-01-01', '2024-01-01')"
            )
            _insert_library_document(conn, "docA", "xxalphatokenxx")
            conn.commit()

            # The prefix a killed first attempt may have committed.
            conn.execute("ALTER TABLE task_state DROP COLUMN heartbeat_at")
            conn.execute(
                "CREATE UNIQUE INDEX uq_task_state_owner_runnable "
                "ON task_state (owner_type, owner_id) "
                "WHERE status IN ('queued', 'running', 'cancelling')"
            )
            conn.execute(
                "CREATE UNIQUE INDEX uq_task_state_owner_parked "
                "ON task_state (owner_type, owner_id) "
                "WHERE status IN ('awaiting_input', 'interaction_consuming', "
                "'interaction_failed')"
            )
            conn.execute(
                "ALTER TABLE background_tasks ADD COLUMN not_before DATETIME"
            )
            for column in (
                "indexed_revision",
                "indexed_generation",
                "index_generation",
            ):
                conn.execute(
                    f"ALTER TABLE library_documents ADD COLUMN {column} INTEGER"
                )
            conn.execute(
                "UPDATE library_documents SET index_generation = 0 "
                "WHERE index_generation IS NULL"
            )
            conn.execute(
                "UPDATE library_documents SET indexed_revision = revision, "
                "indexed_generation = 0 WHERE processing_status = 'completed' "
                "AND indexed_revision IS NULL"
            )
            conn.execute(
                "CREATE TABLE annotation_file_states ("
                "file_path VARCHAR(500) NOT NULL, "
                "revision INTEGER NOT NULL DEFAULT 0, "
                "file_hash VARCHAR(64), updated_at DATETIME NOT NULL, "
                "PRIMARY KEY (file_path))"
            )
            conn.execute(
                "CREATE TABLE annotation_file_transactions ("
                "id VARCHAR(36) NOT NULL, file_path VARCHAR(500) NOT NULL, "
                "expected_revision INTEGER NOT NULL, "
                "expected_file_hash VARCHAR(64) NOT NULL, "
                "new_file_hash VARCHAR(64) NOT NULL, "
                "old_content TEXT NOT NULL, mutations TEXT NOT NULL, "
                "created_at DATETIME NOT NULL, PRIMARY KEY (id))"
            )
            conn.execute(
                "CREATE INDEX ix_annotation_file_transactions_file_path "
                "ON annotation_file_transactions (file_path)"
            )
            conn.execute("DROP INDEX ix_sessions_project_id")
            conn.execute("ALTER TABLE sessions DROP COLUMN project_id")
            conn.execute("DROP INDEX ix_background_tasks_project_id")
            conn.execute(
                "ALTER TABLE background_tasks DROP COLUMN project_id"
            )
            conn.execute(
                "ALTER TABLE library_documents DROP COLUMN embedding_id"
            )
            conn.commit()
        finally:
            conn.close()

        _upgrade_to(db_path, "head")

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0] == "c8a6d03117f9"
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = 's1'"
            ).fetchone()[0] == 1
            assert _fts_search(conn, "alphatoken") == [("docA",)]
        finally:
            conn.close()
        _assert_schema_matches_models(db_path)
