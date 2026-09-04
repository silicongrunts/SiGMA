"""Session deletion removes the temp storage of every descendant session."""

import pytest

import app.services.ai_service as ai_service_module
from tests.ai.conftest import (
    FakeSessionRepo,
    FakeSessionTempService,
    FakeTaskStateRepo,
    make_fake_uow,
)


def _install(monkeypatch, session_repo, task_state_repo, temp):
    uow_cls = make_fake_uow(sessions=session_repo, task_state=task_state_repo)
    monkeypatch.setattr(ai_service_module, "UnitOfWork", uow_cls)
    monkeypatch.setattr(ai_service_module, "session_temp_service", temp)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_session_removes_descendant_temp_dirs(monkeypatch):
    session_repo = FakeSessionRepo(descendants={"s1": ["agent-2", "agent-1", "s1"]})
    temp = FakeSessionTempService()
    _install(monkeypatch, session_repo, FakeTaskStateRepo(), temp)

    await ai_service_module.ai_service.delete_session("p1", "s1")

    # The session repo delete removes the root's and the descendants' rows.
    assert session_repo.deleted == ["s1"]
    # Agent children leave rows via the repo delete; their dirs must go too.
    assert set(temp.deleted) == {"s1", "agent-1", "agent-2"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_session_drains_active_descendant(monkeypatch):
    """An active task on any descendant session is cancelled before delete."""
    session_repo = FakeSessionRepo(descendants={"s1": ["agent-1", "s1"]})
    task_state_repo = FakeTaskStateRepo()
    temp = FakeSessionTempService()

    async def active_for(sid):
        return {"task_id": "task-9"} if sid == "agent-1" else None

    task_state_repo.get_active_by_session = active_for
    _install(monkeypatch, session_repo, task_state_repo, temp)

    cancelled: list[str] = []
    monkeypatch.setattr(
        ai_service_module.task_runtime,
        "cancel",
        lambda task_id: cancelled.append(task_id) or False,
    )
    waited: list[str] = []

    async def fake_wait_for_task(task_id):
        waited.append(task_id)
        return True

    monkeypatch.setattr(
        ai_service_module.task_runtime, "wait_for_task", fake_wait_for_task
    )

    await ai_service_module.ai_service.delete_session("p1", "s1")

    # The descendant's active task was cancelled and drained before delete.
    # The retry loop may re-cancel late detections; the id is what matters.
    assert set(cancelled) == {"task-9"}
    assert waited.count("task-9") >= 1
    assert session_repo.deleted == ["s1"]
    assert set(temp.deleted) == {"s1", "agent-1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_session_keeps_rows_when_temp_cleanup_fails(monkeypatch):
    session_repo = FakeSessionRepo(descendants={"s1": ["agent-1", "s1"]})
    temp = FakeSessionTempService(fail_delete_on="agent-1")
    _install(monkeypatch, session_repo, FakeTaskStateRepo(), temp)

    with pytest.raises(ai_service_module.FileSystemError):
        await ai_service_module.ai_service.delete_session("p1", "s1")

    assert session_repo.deleted == []
    assert temp.deleted == []

    temp.fail_delete_on = None
    await ai_service_module.ai_service.delete_session("p1", "s1")
    assert session_repo.deleted == ["s1"]
