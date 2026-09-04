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
def seeded_ps(ps):
    """The shared ``ps`` service plus one seeded active project.

    Returns ``(service, project_id)``.
    """
    project_id = "test-project-123"
    (ps.USERDATA_DIR / project_id).mkdir()
    ps._update_projects(lambda p: p.update({
        project_id: {"name": "TestProject", "description": "test"},
    }))

    return ps, project_id


# ---------------------------------------------------------------------------
# Full orchestration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_delete_project_calls_all_steps(seeded_ps):
    """delete_project calls each helper method in order."""
    svc, project_id = seeded_ps

    mock_db = MagicMock()

    # One parent recorder: every step mock is attached to it, so a single
    # mock_calls sequence proves the orchestration order, not just counts.
    recorder = MagicMock()

    def _tracked(mock, name):
        recorder.attach_mock(mock, name)
        return mock

    mock_mark_deleting = _tracked(MagicMock(), "mark_project_deleting")
    mock_mark_deleted = _tracked(MagicMock(), "mark_project_deleted")
    mock_cancel = _tracked(AsyncMock(), "_cancel_library_tasks")
    mock_kill = _tracked(AsyncMock(), "_kill_project_kernels")
    mock_db.mark_deleted = _tracked(MagicMock(), "mark_deleted")
    mock_cancel_tasks = _tracked(MagicMock(), "cancel_project")
    mock_evict = _tracked(AsyncMock(), "_evict_project_caches")
    mock_rmdir = _tracked(MagicMock(), "_delete_project_directory")

    with (
        patch.object(svc, 'mark_project_deleting', mock_mark_deleting),
        patch.object(svc, 'mark_project_deleted', mock_mark_deleted),
        patch.object(svc, '_cancel_library_tasks', mock_cancel),
        patch("app.services.task_runtime.cancel_project", mock_cancel_tasks),
        patch.object(svc, '_evict_project_caches', mock_evict),
        patch.object(svc, '_delete_project_directory', mock_rmdir),
        patch.object(svc, '_kill_project_kernels', mock_kill),
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        await svc.delete_project(project_id)

    # Verify call order: barrier first, then drain, then DB tombstone,
    # runner drain, caches, directory, and only then barrier release.
    assert [call[0] for call in recorder.mock_calls] == [
        "mark_project_deleting",
        "_cancel_library_tasks",
        "_kill_project_kernels",      # via _cleanup_project_resources
        "mark_deleted",
        "cancel_project",             # via _cleanup_worker_state
        "_evict_project_caches",
        "_delete_project_directory",
        "mark_project_deleted",
    ]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_delete_project_marks_metadata_deleted(seeded_ps):
    """delete_project keeps a deleted tombstone in projects.json."""
    svc, project_id = seeded_ps

    mock_db = MagicMock()
    mock_db.mark_deleted = MagicMock()
    with (
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
async def test_delete_project_stops_when_worker_drain_times_out(seeded_ps):
    svc, project_id = seeded_ps
    mock_db = MagicMock()
    mock_db.mark_deleted = MagicMock()
    with (
        patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock) as cancel,
        patch.object(svc, '_cleanup_worker_state', new_callable=AsyncMock, return_value=False),
        patch.object(svc, '_delete_project_directory') as rmdir,
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        cancel.return_value = True
        with pytest.raises(Exception) as exc_info:
            await svc.delete_project(project_id)

    assert getattr(exc_info.value, "code", None) == "PROJECT_DRAIN_TIMEOUT"
    rmdir.assert_not_called()
    assert json.loads(svc.PROJECTS_FILE.read_text())[project_id]["status"] == "deleting"


@pytest.mark.asyncio
async def test_delete_project_keeps_barrier_when_kernel_cleanup_fails(seeded_ps):
    svc, project_id = seeded_ps
    with patch.object(svc, '_kill_project_kernels', new_callable=AsyncMock,
                      side_effect=RuntimeError("kernel still running")), \
         patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock), \
         patch.object(svc, '_delete_project_directory') as rmdir, \
         patch("app.database.manager.get_db_manager", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="kernel still running"):
            await svc.delete_project(project_id)

    assert json.loads(svc.PROJECTS_FILE.read_text())[project_id]["status"] == "deleting"
    rmdir.assert_not_called()


@pytest.mark.asyncio
async def test_reset_database_keeps_barrier_when_kernel_cleanup_fails(seeded_ps):
    svc, project_id = seeded_ps
    mock_db = MagicMock()
    mock_db.reset_project_database = AsyncMock()
    with patch.object(svc, '_kill_project_kernels', new_callable=AsyncMock,
                      side_effect=RuntimeError("kernel still running")), \
         patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock), \
         patch("app.database.manager.get_db_manager", new_callable=AsyncMock,
               return_value=mock_db):
        with pytest.raises(RuntimeError, match="kernel still running"):
            await svc.reset_database(project_id)

    assert json.loads(svc.PROJECTS_FILE.read_text())[project_id]["status"] == "resetting"
    mock_db.reset_project_database.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_write_is_blocked_by_project_barrier(seeded_ps, tmp_path):
    svc, project_id = seeded_ps
    from app.services.file_service import file_service

    svc.mark_project_deleting(project_id)
    with pytest.raises(Exception) as exc_info:
        await file_service.write_file_absolute(
            project_id, str(svc.USERDATA_DIR / project_id / "late.txt"), "late",
        )

    assert getattr(exc_info.value, "code", None) == "PROJECT_LIFECYCLE_BLOCKED"
    assert not (svc.USERDATA_DIR / project_id / "late.txt").exists()


@pytest.mark.asyncio
async def test_lifecycle_reconcile_converges_crashed_barriers(seeded_ps):
    """Startup makes interrupted delete/reset operations visible again."""
    svc, project_id = seeded_ps
    reset_id = "reset-project"
    (svc.USERDATA_DIR / reset_id).mkdir()
    svc._update_projects(lambda projects: projects.update({
        project_id: {"name": "Deleting", "status": "deleting"},
        reset_id: {"name": "Resetting", "status": "resetting"},
    }))

    db_manager = MagicMock()
    db_manager.reset_project_database = AsyncMock()
    with patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock) as cancel_library, \
         patch.object(svc, '_cleanup_project_resources', new_callable=AsyncMock) as cleanup, \
         patch.object(svc, '_cleanup_worker_state', new_callable=AsyncMock, return_value=True) as cleanup_worker, \
         patch.object(svc, '_evict_project_caches', new_callable=AsyncMock), \
         patch("app.database.manager.get_db_manager", new_callable=AsyncMock,
               return_value=db_manager):
        await svc.reconcile_lifecycle()

    projects = json.loads(svc.PROJECTS_FILE.read_text())
    assert projects[project_id]["status"] == "deleted"
    assert projects[reset_id]["status"] == "active"
    assert not (svc.USERDATA_DIR / project_id).exists()
    assert (svc.USERDATA_DIR / reset_id).exists()
    assert cancel_library.await_args_list == [((project_id,),), ((reset_id,),)]
    assert cleanup.await_args_list == [((project_id,),), ((reset_id,),)]
    assert cleanup_worker.await_args_list == [((project_id,),), ((reset_id,),)]


@pytest.mark.asyncio
async def test_reconcile_keeps_barrier_when_resource_cleanup_fails(seeded_ps):
    svc, project_id = seeded_ps
    reset_id = "reset-project"
    (svc.USERDATA_DIR / reset_id).mkdir()
    svc._update_projects(lambda projects: projects.update({
        project_id: {"name": "Deleting", "status": "deleting"},
        reset_id: {"name": "Resetting", "status": "resetting"},
    }))

    async def fail_cleanup(pid):
        raise RuntimeError(f"resources remain for {pid}")

    db_manager = MagicMock()
    db_manager.reset_project_database = AsyncMock()
    with patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock), \
         patch.object(svc, '_cleanup_project_resources', new_callable=AsyncMock,
                      side_effect=fail_cleanup), \
         patch("app.database.manager.get_db_manager", new_callable=AsyncMock,
               return_value=db_manager):
        await svc.reconcile_lifecycle()

    projects = json.loads(svc.PROJECTS_FILE.read_text())
    assert projects[project_id]["status"] == "deleting"
    assert projects[reset_id]["status"] == "resetting"
    db_manager.mark_deleted.assert_not_called()
    db_manager.reset_project_database.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_delete_project_removes_directory(seeded_ps):
    """delete_project deletes the project directory."""
    svc, project_id = seeded_ps
    project_path = svc.USERDATA_DIR / project_id
    assert project_path.is_dir()

    mock_db = MagicMock()
    mock_db.mark_deleted = MagicMock()
    with (
        patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock),
        patch.object(svc, '_evict_project_caches', new_callable=AsyncMock),
        patch.object(svc, '_kill_project_kernels', new_callable=AsyncMock),
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        await svc.delete_project(project_id)

    assert not project_path.exists()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_reset_database_uses_barrier_and_restores_active(seeded_ps):
    """reset_database blocks new project work while DB files are removed."""
    svc, project_id = seeded_ps

    mock_db = MagicMock()
    mock_db.reset_project_database = AsyncMock()

    async def _assert_resetting(pid):
        assert pid == project_id
        assert not svc.is_project_active(project_id)
        data = json.loads(svc.PROJECTS_FILE.read_text())
        assert data[project_id]["status"] == "resetting"

    mock_db.reset_project_database.side_effect = _assert_resetting

    with (
        patch.object(svc, '_cancel_library_tasks', new_callable=AsyncMock) as mock_cancel,
        patch.object(svc, '_evict_project_caches', new_callable=AsyncMock) as mock_evict,
        patch("app.services.task_runtime.cancel_project") as mock_cancel_tasks,
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        await svc.reset_database(project_id)

    mock_cancel.assert_awaited_once_with(project_id)
    mock_cancel_tasks.assert_called_once_with(project_id)
    mock_evict.assert_awaited_once_with(project_id, mock_db)
    mock_db.reset_project_database.assert_awaited_once_with(project_id)

    data = json.loads(svc.PROJECTS_FILE.read_text())
    assert data[project_id]["status"] == "active"
    assert svc.is_project_active(project_id)


@pytest.mark.asyncio
async def test_reset_library_drain_failure_keeps_resetting_barrier(seeded_ps):
    svc, project_id = seeded_ps
    mock_db = MagicMock()
    mock_db.reset_project_database = AsyncMock()
    with (
        patch.object(
            svc, '_cancel_library_tasks', new_callable=AsyncMock,
            side_effect=RuntimeError("library drain failed"),
        ),
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
    ):
        with pytest.raises(RuntimeError, match="library drain failed"):
            await svc.reset_database(project_id)

    assert json.loads(svc.PROJECTS_FILE.read_text())[project_id]["status"] == "resetting"
    mock_db.reset_project_database.assert_not_awaited()


# ---------------------------------------------------------------------------
# Private method tests
# ---------------------------------------------------------------------------

def test_delete_project_directory(seeded_ps):
    """_delete_project_directory removes the directory from disk."""
    svc, project_id = seeded_ps
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
    """A deleted registry tombstone prevents ghost DB/directory recreation.

    The project directory still exists on disk; the "deleted" status in the
    registry (read via ``settings.USERDATA_DIR`` by ``project_registry``) is
    what must keep ``ensure_db_exists`` from recreating the database.
    """
    from app.core import config as config_module
    from app.core import project_registry

    sigma_dir = tmp_path / ".SiGMA"
    sigma_dir.mkdir(parents=True, exist_ok=True)
    projects_file = sigma_dir / "projects.json"
    projects_file.write_text(
        json.dumps({"ghost": {"name": "Ghost", "status": "deleted"}}),
        encoding="utf-8",
    )
    ghost_dir = tmp_path / "ghost"
    (ghost_dir / ".SiGMA").mkdir(parents=True)

    monkeypatch.setattr(config_module, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config_module, "SIGMA_DIR", sigma_dir)
    monkeypatch.setattr(project_registry, "_registry_cache", None)
    monkeypatch.setattr(project_registry, "_registry_mtime", 0.0)

    manager = DatabaseManager()
    with pytest.raises(ProjectNotFoundError):
        await manager.ensure_db_exists("ghost")

    assert ghost_dir.is_dir()
    assert not (ghost_dir / ".SiGMA" / "project_data.db").exists()


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
async def test_cancel_library_tasks_cancels_project_level_once():
    """The project-level cancel covers every active task row; no per-document
    cancellation runs on top of it."""
    svc = ProjectService()
    mock_bg = MagicMock()
    mock_bg.cancel_project_tasks = AsyncMock()
    mock_bg.cancel_document_tasks = AsyncMock()
    with patch("app.services.background_task_service.background_task_service", mock_bg):
        assert await svc._cancel_library_tasks("proj1") is True
    mock_bg.cancel_project_tasks.assert_awaited_once_with("proj1")
    mock_bg.cancel_document_tasks.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_kill_project_kernels_handles_failure():
    """_kill_project_kernels propagates service failures."""
    svc = ProjectService()
    with patch("app.services.jupyter_service.get_jupyter", side_effect=Exception("no jupyter")):
        with pytest.raises(Exception, match="no jupyter"):
            await svc._kill_project_kernels("test")


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_running_task_lifecycle_survives_deleting_registry_status(project, monkeypatch):
    """A late library handler cannot finalize after the lifecycle barrier."""
    from app.core import config as config_module
    from app.core import project_registry
    from app.database.unit_of_work import UnitOfWork
    from app.services import background_task_service as bts
    from app.services import library_task_protocol
    from app.services.library_task_protocol import RunningTaskContext

    # A claimed task row in the project's real database, with a trivially
    # succeeding handler registered for its kind.
    async def handler(ctx, _payload):
        pass

    kind = "project_delete_lifecycle_test"
    # The registry has no unregister API; monkeypatch removes the entry
    # again so the global handler registry stays unpolluted after the test.
    monkeypatch.setitem(library_task_protocol._handlers, kind, handler)

    task_id = await bts.background_task_service.enqueue(
        project_id=project, kind=kind, payload={}, wake=False,
    )
    async with UnitOfWork(project) as uow:
        claimed = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert claimed is not None and claimed.id == task_id

    # Flip the registry the way mark_project_deleting does (DB still on disk).
    projects_file = config_module.USERDATA_DIR / ".SiGMA" / "projects.json"
    data = json.loads(projects_file.read_text())
    data[project]["status"] = "deleting"
    projects_file.write_text(json.dumps(data))
    monkeypatch.setattr(project_registry, "_registry_cache", None)
    monkeypatch.setattr(project_registry, "_registry_mtime", 0.0)
    assert not project_registry.is_project_active(project)

    ctx = RunningTaskContext(
        project_id=project, task_id=claimed.id,
        owner=claimed.lease_owner, lease_seconds=60,
    )
    assert await ctx.heartbeat() is False
    assert await ctx.is_cancelling() is True

    await bts.library_task_runner._run_one(project, claimed)

    async with UnitOfWork(project, allow_inactive=True) as uow:
        row = await uow.background_tasks.get_by_id(claimed.id)
    assert row.status == "running"
