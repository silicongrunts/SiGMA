import pytest

import app.services.ai_service as ai_service_module


class _FakeUow:
    """Minimal async context manager exposing a fake task_state repo."""

    def __init__(self, task_state):
        self.task_state = task_state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeTaskStateRepo:
    def __init__(self):
        self.completed = []

    async def get_active_by_session(self, session_id):
        return {
            "task_id": "task-1",
            "session_id": session_id,
            "task_type": "llm_chat",
        }

    async def check_liveness(self, task_id):
        return "stale"

    async def mark_completed(self, task_id):
        self.completed.append(task_id)


class FailingTaskStateRepo:
    async def get_active_by_session(self, session_id):
        raise RuntimeError("database unavailable")


@pytest.mark.asyncio
async def test_get_active_task_reports_stale_without_marking_completed(monkeypatch):
    fake_repo = FakeTaskStateRepo()
    monkeypatch.setattr(
        ai_service_module, "UnitOfWork", lambda pid: _FakeUow(fake_repo),
    )

    result = await ai_service_module.ai_service.get_active_task("project-1", "session-1")

    assert result["active"] is False
    assert result["task_id"] == "task-1"
    assert result["status"] == "stale"
    assert result["recoverable"] is True
    assert fake_repo.completed == []


@pytest.mark.asyncio
async def test_get_active_task_reports_unknown_on_read_failure(monkeypatch):
    monkeypatch.setattr(
        ai_service_module, "UnitOfWork", lambda pid: _FakeUow(FailingTaskStateRepo()),
    )

    result = await ai_service_module.ai_service.get_active_task("project-1", "session-1")

    assert result["active"] is False
    assert result["task_id"] is None
    assert result["status"] == "unknown"
    assert result["session_id"] == "session-1"
    assert result["recoverable"] is True
