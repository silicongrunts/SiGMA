"""
Tests for task_service — verifies the service layer correctly wraps
TaskRepository via UnitOfWork.
"""

import pytest

from app.database.models import Session


def _make_mock_manager(db_session_factory):
    """Create a mock DatabaseManager that delegates to the test engine."""
    from unittest.mock import AsyncMock

    mock_manager = AsyncMock()
    mock_manager.ensure_db_exists = AsyncMock()

    async def get_session(project_id, *, allow_inactive=False):
        return db_session_factory()

    mock_manager.get_session = get_session
    return mock_manager


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_create_task(db_session_factory):
    """task_service.create_task creates a task with seq=0."""
    from unittest.mock import patch
    from app.services.task_service import task_service

    session_id = "test-session-1"
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test-project"))
        await s.commit()

    mock_manager = _make_mock_manager(db_session_factory)

    with patch("app.database.unit_of_work.get_db_manager", return_value=mock_manager):
        task = await task_service.create_task(
            project_id="test-project",
            session_id=session_id,
            subject="Test task",
            description="A test",
        )

    assert task is not None
    assert task.subject == "Test task"
    assert task.seq == 0


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_replace_tasks(db_session_factory):
    """replace_tasks atomically replaces all tasks for a session."""
    from unittest.mock import patch
    from app.services.task_service import task_service

    session_id = "test-session-2"
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test-project"))
        await s.commit()

    mock_manager = _make_mock_manager(db_session_factory)

    with patch("app.database.unit_of_work.get_db_manager", return_value=mock_manager):
        # Create initial tasks
        await task_service.create_task("p", session_id, "old_1")
        await task_service.create_task("p", session_id, "old_2")

        # Replace with new set
        tasks = await task_service.replace_tasks("p", session_id, [
            {"content": "new_a", "status": "pending"},
            {"content": "new_b", "status": "in_progress"},
            {"content": "new_c", "status": "completed"},
        ])

    assert len(tasks) == 3
    assert [t.seq for t in tasks] == [0, 1, 2]
    assert [t.subject for t in tasks] == ["new_a", "new_b", "new_c"]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_resolve_task(db_session_factory):
    """resolve_task finds a task by prefix."""
    from unittest.mock import patch
    from app.services.task_service import task_service

    session_id = "test-session-3"
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test-project"))
        await s.commit()

    mock_manager = _make_mock_manager(db_session_factory)

    with patch("app.database.unit_of_work.get_db_manager", return_value=mock_manager):
        created = await task_service.create_task("p", session_id, "Resolve me")

        # Resolve by full ID
        task, err = await task_service.resolve_task("p", session_id, created.id)
        assert task is not None
        assert err is None
        assert task.subject == "Resolve me"

        # Resolve by prefix (8+ chars)
        prefix = created.id[:8]
        task2, err2 = await task_service.resolve_task("p", session_id, prefix)
        assert task2 is not None
        assert err2 is None


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_update_task(db_session_factory):
    """update_task modifies task fields."""
    from unittest.mock import patch
    from app.services.task_service import task_service

    session_id = "test-session-4"
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test-project"))
        await s.commit()

    mock_manager = _make_mock_manager(db_session_factory)

    with patch("app.database.unit_of_work.get_db_manager", return_value=mock_manager):
        created = await task_service.create_task("p", session_id, "Original")
        updated = await task_service.update_task(
            "p", created.id, status="in_progress", subject="Renamed"
        )
        cleaned_count = 0  # update_task no longer returns a cleanup count

    assert updated.subject == "Renamed"
    assert updated.status == "in_progress"
    assert cleaned_count == 0  # status != "completed" → no auto-cleanup


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_list_active_tasks(db_session_factory):
    """list_active_tasks returns non-deleted tasks."""
    from unittest.mock import patch
    from app.services.task_service import task_service

    session_id = "test-session-5"
    async with db_session_factory() as s:
        s.add(Session(id=session_id, project_id="test-project"))
        await s.commit()

    mock_manager = _make_mock_manager(db_session_factory)

    with patch("app.database.unit_of_work.get_db_manager", return_value=mock_manager):
        await task_service.create_task("p", session_id, "Task A")
        await task_service.create_task("p", session_id, "Task B")

        tasks = await task_service.list_active_tasks("p", session_id)

    assert len(tasks) == 2
    subjects = [t.subject for t in tasks]
    assert "Task A" in subjects
    assert "Task B" in subjects
