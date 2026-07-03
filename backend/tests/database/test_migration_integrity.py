"""Drift sentinel: verify that ``alembic upgrade head`` produces a schema
that exactly matches ``Base.metadata``.

This test is the structural safeguard against the model/migration drift that
previously caused ``library_documents.revision`` and the entire
``background_tasks`` table to go missing from migrated databases (while
``create_all``-based tests passed because they never exercised the migration
path).

If this test fails, it means someone changed a model without writing the
corresponding alembic migration (or vice-versa).
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest
from alembic.config import Config
from alembic import command
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine

from app.database.models import Base


BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_DIR = BACKEND_DIR / "alembic"


def _build_fresh_db(db_path: Path) -> None:
    """Create a fresh database via ``alembic upgrade head``."""
    alembic_ini = BACKEND_DIR / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option(
        "sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}",
    )
    command.upgrade(cfg, "head")


def _is_fts_or_alembic(name: str) -> bool:
    """True for FTS5 virtual/shadow tables, triggers, and alembic's own table."""
    return (
        name.endswith("_fts")
        or "_fts_" in name
        or name == "alembic_version"
    )


def _filter_irrelevant_diffs(diffs):
    """Strip diffs for FTS5 shadow tables and alembic_version.

    ``compare_metadata`` sees FTS5 shadow tables (``_data``, ``_idx``, etc.)
    and the ``alembic_version`` table as extra tables in the DB that are not
    in ``Base.metadata``.  These are expected — the migration creates them
    intentionally.
    """
    result = []
    for diff in diffs:
        if not diff:
            result.append(diff)
            continue
        op = diff[0]
        # remove_table: DB has a table the model doesn't define
        if op == "remove_table":
            tname = diff[1].name if hasattr(diff[1], "name") else str(diff[1])
            if _is_fts_or_alembic(tname):
                continue
        result.append(diff)
    return result


def test_migration_schema_matches_models():
    """``upgrade head`` must produce a schema identical to ``Base.metadata``."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "integrity.db"
        _build_fresh_db(db_path)

        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as conn:
                ctx = MigrationContext.configure(
                    conn,
                    opts={
                        "compare_type": True,
                        # compare_server_default is intentionally disabled:
                        # SQLite represents TEXT server defaults as TextClause
                        # while the model uses a literal string, causing a false
                        # positive.  Only project_config.value has a server_default.
                    },
                )
                diffs = compare_metadata(ctx, Base.metadata)
        finally:
            engine.dispose()

    relevant = _filter_irrelevant_diffs(diffs)
    if relevant:
        formatted = "\n".join(repr(d) for d in relevant)
        pytest.fail(
            f"Schema drift detected — model and migration are out of sync:\n{formatted}"
        )


def test_fts5_triggers_present_after_head():
    """FTS5 sync triggers must exist after ``upgrade head``.

    This is the structural safeguard against the most fragile migration
    pattern: ``batch_alter_table('library_documents')`` drops and recreates
    the table, which destroys the FTS5 triggers.  If a future migration
    forgets to recreate them, the FTS5 index silently stops updating and
    keyword search returns stale results.

    Because this test runs the full migration chain (``upgrade head`` from
    empty), any migration in the chain that drops triggers without
    restoring them will cause this test to fail.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fts.db"
        _build_fresh_db(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            triggers = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }

            # Functional check: insert a document and verify the FTS index
            # picks it up — this catches not just missing triggers but also
            # triggers pointing at the wrong table or columns.
            conn.execute(
                "INSERT INTO sessions (id, project_id, title, session_kind, "
                "parent_tool_call_id, created_at, updated_at, is_archived) "
                "VALUES ('s1', 'p1', 'test', 'chat', '', '2024-01-01', '2024-01-01', 0)"
            )
            conn.execute(
                "INSERT INTO library_documents "
                "(id, title, description, content, doc_type, revision, "
                " processing_status, processing_log, is_folder, "
                " created_at, updated_at) "
                "VALUES ('d1', 'quantum computing', '', 'entanglement theory', 'text', "
                " 1, 'completed', '', 0, '2024-01-01', '2024-01-01')"
            )
            fts_row = conn.execute(
                "SELECT title FROM library_documents_fts WHERE title = 'quantum computing'"
            ).fetchone()
            trigram_row = conn.execute(
                "SELECT title FROM library_documents_fts "
                "WHERE library_documents_fts MATCH 'uantum'"
            ).fetchone()
        finally:
            conn.close()

    # --- Virtual table and shadow tables ---
    assert "library_documents_fts" in tables, "FTS5 virtual table missing"
    for suffix in ("_data", "_idx", "_docsize", "_config"):
        assert f"library_documents_fts{suffix}" in tables, (
            f"FTS5 shadow table library_documents_fts{suffix} missing"
        )

    # --- Sync triggers (the fragile part) ---
    for trigger in ("library_documents_ai", "library_documents_ad", "library_documents_au"):
        assert trigger in triggers, (
            f"FTS5 trigger {trigger} missing — a future migration likely did "
            f"batch_alter_table('library_documents') without recreating FTS5 "
            f"triggers."
        )

    # --- Functional: triggers actually sync inserts into FTS index ---
    assert fts_row is not None, (
        "FTS5 insert trigger fired but document not found in index — "
        "trigger exists but may reference wrong columns/table."
    )
    assert trigram_row is not None, (
        "FTS5 index is present but does not support substring search; "
        "library keyword search requires the trigram tokenizer."
    )
