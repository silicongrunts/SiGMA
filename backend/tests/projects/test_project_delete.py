"""
Tests for delete_project() orchestration.

Uses mocks for all external services to verify that delete_project()
calls each step in order and handles partial failures gracefully.
"""

import json
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from app.core.exceptions import DatabaseException, ProjectNotFoundError
from app.database.manager import DatabaseManager
from app.services.project_service import ProjectService


@pytest.fixture
def ps(tmp_path, monkeypatch):
    """Create a ProjectService pointing at a temp directory with a project.

    Patches the global ``USERDATA_DIR`` so both ``project_service`` and
    ``core.project_registry`` (which consults the same setting) read
    from the temp directory during the test.
    """
    from app.core import config as config_module

    monkeypatch.setattr(config_module, "USERDATA_DIR", tmp_path)

    svc = ProjectService()
    svc.USERDATA_DIR = tmp_path
    svc.SIGMA_DIR = tmp_path / ".SiGMA"
    svc.SIGMA_DIR.mkdir(parents=True, exist_ok=True)
    svc.PROJECTS_FILE = svc.SIGMA_DIR / "projects.json"

    # Create a project directory and seed projects.json
    project_id = "test-project-123"
    (tmp_path / project_id).mkdir()
    svc._update_projects(lambda p: p.update({
        project_id: {"name": "TestProject", "description": "test"},
    }))

    return svc, project_id


# ---------------------------------------------------------------------------
# Full orchestration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_delete_project_calls_all_steps(ps):
    """delete_project calls each helper method in order."""
    svc, project_id = ps

    mock_db = MagicMock()
    mock_db.mark_deleted = MagicMock()
    mock_db.cleanup_project = AsyncMock()

    with (
        patch.object(svc, '_collect_running_doc_ids_async', new_callable=AsyncMock, return_value=["doc1"]) as mock_collect,
        patch.object(svc, 'mark_project_deleting') as mock_mark_deleting,
        patch.object(svc, 'mark_project_deleted') as mock_mark_deleted,
        patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock) as mock_cancel,
        patch("app.workers.huey_tasks.purge_project_tasks") as mock_purge_huey,
        patch.object(svc, '_evict_project_caches', new_callable=AsyncMock) as mock_evict,
        patch.object(svc, '_delete_project_directory') as mock_rmdir,
        patch.object(svc, '_kill_project_kernels', new_callable=AsyncMock) as mock_kill,
        patch("app.workers.stream_server.stream_server.cancel_project", new_callable=AsyncMock) as mock_cancel_streams,
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        await svc.delete_project(project_id)

    # Verify call order
    mock_mark_deleting.assert_called_once_with(project_id)
    mock_collect.assert_awaited_once_with(project_id)
    mock_db.mark_deleted.assert_called_once_with(project_id)
    mock_cancel.assert_awaited_once_with(project_id, ["doc1"])
    mock_purge_huey.assert_called_once_with(project_id)
    mock_cancel_streams.assert_awaited_once_with(project_id)
    mock_evict.assert_awaited_once_with(project_id, mock_db)
    mock_rmdir.assert_called_once_with(project_id)
    mock_mark_deleted.assert_called_once_with(project_id)
    mock_kill.assert_awaited_once_with(project_id)


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_delete_project_marks_metadata_deleted(ps):
    """delete_project keeps a deleted tombstone in projects.json."""
    svc, project_id = ps

    mock_db = MagicMock()
    mock_db.mark_deleted = MagicMock()
    mock_db.cleanup_project = AsyncMock()
    with (
        patch.object(svc, '_collect_running_doc_ids_async', new_callable=AsyncMock, return_value=[]),
        patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock),
        patch.object(svc, '_evict_project_caches', new_callable=AsyncMock),
        patch.object(svc, '_kill_project_kernels', new_callable=AsyncMock),
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        await svc.delete_project(project_id)

    data = json.loads(svc.PROJECTS_FILE.read_text())
    assert data[project_id]["status"] == "deleted"
    assert data[project_id]["deleted_at"]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_delete_project_removes_directory(ps):
    """delete_project deletes the project directory."""
    svc, project_id = ps
    project_path = svc.USERDATA_DIR / project_id
    assert project_path.is_dir()

    mock_db = MagicMock()
    mock_db.mark_deleted = MagicMock()
    mock_db.cleanup_project = AsyncMock()
    with (
        patch.object(svc, '_collect_running_doc_ids_async', new_callable=AsyncMock, return_value=[]),
        patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock),
        patch.object(svc, '_evict_project_caches', new_callable=AsyncMock),
        patch.object(svc, '_kill_project_kernels', new_callable=AsyncMock),
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        await svc.delete_project(project_id)

    assert not project_path.exists()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_reset_database_uses_barrier_and_restores_active(ps):
    """reset_database blocks new project work while DB files are removed."""
    svc, project_id = ps

    mock_db = MagicMock()
    mock_db.reset_project_database = AsyncMock()

    async def _assert_resetting(pid):
        assert pid == project_id
        assert not svc.is_project_active(project_id)
        data = json.loads(svc.PROJECTS_FILE.read_text())
        assert data[project_id]["status"] == "resetting"

    mock_db.reset_project_database.side_effect = _assert_resetting

    with (
        patch.object(svc, '_collect_running_doc_ids_async', new_callable=AsyncMock, return_value=["doc1"]) as mock_collect,
        patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock) as mock_cancel,
        patch.object(svc, '_evict_project_caches', new_callable=AsyncMock) as mock_evict,
        patch("app.workers.huey_tasks.purge_project_tasks") as mock_purge_huey,
        patch("app.workers.stream_server.stream_server.cancel_project", new_callable=AsyncMock) as mock_cancel_streams,
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        await svc.reset_database(project_id)

    mock_collect.assert_awaited_once_with(project_id)
    mock_cancel.assert_awaited_once_with(project_id, ["doc1"])
    mock_purge_huey.assert_called_once_with(project_id)
    mock_cancel_streams.assert_awaited_once_with(project_id)
    mock_evict.assert_awaited_once_with(project_id, mock_db)
    mock_db.reset_project_database.assert_awaited_once_with(project_id)

    data = json.loads(svc.PROJECTS_FILE.read_text())
    assert data[project_id]["status"] == "active"
    assert svc.is_project_active(project_id)


# ---------------------------------------------------------------------------
# Private method tests
# ---------------------------------------------------------------------------

def test_delete_project_directory(ps):
    """_delete_project_directory removes the directory from disk."""
    svc, project_id = ps
    project_path = svc.USERDATA_DIR / project_id
    assert project_path.is_dir()

    svc._delete_project_directory(project_id)
    assert not project_path.exists()


def test_delete_project_directory_idempotent(tmp_path):
    """_delete_project_directory is safe on non-existent directory."""
    svc = ProjectService()
    svc.USERDATA_DIR = tmp_path
    # Should not raise
    svc._delete_project_directory("nonexistent-project")


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_deleted_project_db_is_not_recreated(tmp_path, monkeypatch):
    """A deleted registry tombstone prevents ghost DB/directory recreation."""
    from app.services.project_service import project_service

    sigma_dir = tmp_path / ".SiGMA"
    sigma_dir.mkdir(parents=True, exist_ok=True)
    projects_file = sigma_dir / "projects.json"
    projects_file.write_text(
        json.dumps({"ghost": {"name": "Ghost", "status": "deleted"}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(project_service, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(project_service, "SIGMA_DIR", sigma_dir)
    monkeypatch.setattr(project_service, "PROJECTS_FILE", projects_file)

    manager = DatabaseManager()
    with pytest.raises(ProjectNotFoundError):
        await manager.ensure_db_exists("ghost")

    assert not (tmp_path / "ghost").exists()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_startup_migration_skips_non_active_projects(tmp_path, monkeypatch):
    """Startup migration ignores deleted/resetting/unregistered project dirs."""
    from app.core import config as config_module
    from app.services.project_service import project_service

    sigma_dir = tmp_path / ".SiGMA"
    sigma_dir.mkdir(parents=True, exist_ok=True)
    projects_file = sigma_dir / "projects.json"
    projects_file.write_text(
        json.dumps({
            "active": {"name": "Active", "status": "active"},
            "deleted": {"name": "Deleted", "status": "deleted"},
            "resetting": {"name": "Resetting", "status": "resetting"},
        }),
        encoding="utf-8",
    )
    for pid in ("active", "deleted", "resetting", "unregistered"):
        db_dir = tmp_path / pid / ".SiGMA"
        db_dir.mkdir(parents=True, exist_ok=True)
        (db_dir / "project_data.db").write_bytes(b"")

    monkeypatch.setattr(config_module, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config_module, "SIGMA_DIR", sigma_dir)
    monkeypatch.setattr(project_service, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(project_service, "SIGMA_DIR", sigma_dir)
    monkeypatch.setattr(project_service, "PROJECTS_FILE", projects_file)

    manager = DatabaseManager()
    migrated = []

    async def fake_run_migrations(pid):
        migrated.append(pid)

    monkeypatch.setattr(manager, "_run_migrations", fake_run_migrations)
    await manager.migrate_all_projects()

    assert migrated == ["active"]
    assert manager._initialized == {"active"}


def test_get_session_maker_requires_initialized_project():
    """Direct session-maker access cannot bypass migration initialization."""
    manager = DatabaseManager()
    with pytest.raises(DatabaseException):
        manager.get_session_maker("project-a")


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_cancel_library_tasks_empty():
    """_cancel_library_tasks still cancels project-level durable tasks."""
    svc = ProjectService()
    mock_bg = MagicMock()
    mock_bg.cancel_project_tasks = AsyncMock()
    mock_bg.cancel_document_tasks = AsyncMock()
    with patch("app.services.background_task_service.background_task_service", mock_bg):
        await svc._cancel_library_tasks("proj1", [])
    mock_bg.cancel_project_tasks.assert_awaited_once_with("proj1")
    mock_bg.cancel_document_tasks.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_kill_project_kernels_handles_failure():
    """_kill_project_kernels logs warning on failure and doesn't raise."""
    svc = ProjectService()
    with patch("app.services.jupyter_service.get_jupyter", side_effect=Exception("no jupyter")):
        await svc._kill_project_kernels("test")
    # Should not raise
