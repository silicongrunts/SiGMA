import pytest

from app.database.manager import DatabaseManager


@pytest.mark.asyncio
async def test_cleanup_inactive_projects_disposes_cached_engines(monkeypatch):
    manager = DatabaseManager()
    disposed = []

    class FakeEngine:
        async def dispose(self):
            disposed.append("old-project")

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
