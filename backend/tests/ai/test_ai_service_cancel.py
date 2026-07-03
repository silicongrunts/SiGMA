import pytest
from sqlalchemy.exc import OperationalError

import app.services.ai_service as ai_service_module
import app.workers.stream_server as stream_server_module


class _FakeUow:
    """Minimal async context manager exposing a fake task_state repo."""

    def __init__(self, task_state):
        self.task_state = task_state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeTaskStateRepo:
    def __init__(self, cancel_status):
        self._cancel_status = cancel_status
        self.requested = []

    async def request_cancel(self, task_id):
        self.requested.append(task_id)
        return self._cancel_status


class _FakeStreamServer:
    def __init__(self):
        self.cancel_calls = []
        self.fail = False

    async def cancel_task(self, task_id):
        self.cancel_calls.append(task_id)
        if self.fail:
            raise RuntimeError("tcp unavailable")


def _patch(monkeypatch, repo, stream=None):
    monkeypatch.setattr(ai_service_module, "UnitOfWork", lambda pid: _FakeUow(repo))
    monkeypatch.setattr(stream_server_module, "stream_server", stream or _FakeStreamServer())


@pytest.mark.asyncio
async def test_cancel_task_returns_truthful_result_for_cancelling(monkeypatch):
    repo = _FakeTaskStateRepo("cancelling")
    stream = _FakeStreamServer()
    _patch(monkeypatch, repo, stream)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelling", "task_id": "task-1"}
    assert repo.requested == ["task-1"]
    assert stream.cancel_calls == ["task-1"]  # TCP fast-path still invoked


@pytest.mark.asyncio
async def test_cancel_task_returns_truthful_result_for_awaiting_input(monkeypatch):
    repo = _FakeTaskStateRepo("cancelled")
    _patch(monkeypatch, repo)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelled", "task_id": "task-1"}


@pytest.mark.asyncio
async def test_cancel_task_reports_not_cancelled_for_terminal(monkeypatch):
    repo = _FakeTaskStateRepo("completed")
    _patch(monkeypatch, repo)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": False, "status": "completed", "task_id": "task-1"}


@pytest.mark.asyncio
async def test_cancel_task_reports_not_cancelled_for_missing(monkeypatch):
    repo = _FakeTaskStateRepo("not_found")
    _patch(monkeypatch, repo)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": False, "status": "not_found", "task_id": "task-1"}


@pytest.mark.asyncio
async def test_cancel_task_tolerates_tcp_failure(monkeypatch):
    """A TCP failure must never mask the database truth."""
    repo = _FakeTaskStateRepo("cancelling")
    stream = _FakeStreamServer()
    stream.fail = True
    _patch(monkeypatch, repo, stream)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelling", "task_id": "task-1"}


class _LockThenSucceedRepo:
    """request_cancel raises a locked-DB error once, then succeeds."""

    def __init__(self):
        self.calls = 0

    async def request_cancel(self, task_id):
        self.calls += 1
        if self.calls == 1:
            raise OperationalError("UPDATE task_state", {}, Exception("database is locked"))
        return "cancelling"


@pytest.mark.asyncio
async def test_cancel_task_retries_on_locked_db(monkeypatch):
    """A transient locked-DB OperationalError must be retried, not surfaced as
    a 500, because the compare-and-swap cancel is idempotent."""
    repo = _LockThenSucceedRepo()
    monkeypatch.setattr(ai_service_module, "UnitOfWork", lambda pid: _FakeUow(repo))
    monkeypatch.setattr(stream_server_module, "stream_server", _FakeStreamServer())

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelling", "task_id": "task-1"}
    assert repo.calls == 2  # first attempt locked, second succeeded
