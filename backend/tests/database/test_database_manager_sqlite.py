import sqlite3

from app.database.manager import _configure_sqlite_file


def test_sqlite_file_configuration_uses_wal(tmp_path):
    db_path = tmp_path / "project_data.db"

    _configure_sqlite_file(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    assert journal_mode.lower() == "wal"
