import pytest

from app.core import config as config_module
from app.database.manager import DatabaseManager


@pytest.mark.asyncio
async def test_cleanup_inactive_projects_disposes_cached_engines(monkeypatch):
    manager = DatabaseManager()
    disposed = []

    class FakeEngine:
        async def dispose(self):
            disposed.append("old-project")

    # Seeding the private caches directly: DatabaseManager has no public
    # registration API — engines/makers are only created (against a real
    # migrated DB file) inside ensure_db_exists. This test is a unit check
    # of cache eviction, so fake entries in the exact containers that
    # cleanup_inactive_projects walks are the faithful, cheap simulation.
    manager._initialized.add("old-project")
    manager._engines["old-project"] = FakeEngine()
    manager._makers["old-project"] = object()

    monkeypatch.setattr(
        "app.core.project_registry.is_project_active",
        lambda project_id: project_id != "old-project",
    )

    removed = await manager.cleanup_inactive_projects()

    assert removed == ["old-project"]
    assert disposed == ["old-project"]
    assert "old-project" not in manager._initialized
    assert "old-project" not in manager._engines
    assert "old-project" not in manager._makers


@pytest.mark.asyncio
async def test_reset_unlinks_unreadable_database_after_disposing_engine(tmp_path, monkeypatch):
    root = tmp_path / "userdata"
    db_path = root / "project-a" / ".SiGMA" / "project_data.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(config_module, "USERDATA_DIR", root)

    manager = DatabaseManager()
    disposed_while_file_exists = []

    class FakeEngine:
        async def dispose(self):
            disposed_while_file_exists.append(db_path.exists())

    manager._initialized.add("project-a")
    manager._engines["project-a"] = FakeEngine()
    manager._makers["project-a"] = object()

    await manager.reset_project_database("project-a")

    assert disposed_while_file_exists == [True]
    assert not db_path.exists()
    assert not db_path.with_name("project_data.db-wal").exists()
    assert not db_path.with_name("project_data.db-shm").exists()
