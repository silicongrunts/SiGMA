"""ai_service.get_active_task: active-row reporting, idle shape, and
transient-read retry behavior against a fake task_state repo."""

import pytest
from sqlalchemy.exc import OperationalError

import app.services.ai_service as ai_service_module
from app.core.exceptions import TaskStateUnavailableError
from tests.ai.conftest import FakeTaskStateRepo, make_fake_uow


def _patch_repo(monkeypatch, repo):
    monkeypatch.setattr(
        ai_service_module, "UnitOfWork", make_fake_uow(task_state=repo),
    )


@pytest.mark.asyncio
async def test_get_active_task_reports_running_row(monkeypatch):
    _patch_repo(monkeypatch, FakeTaskStateRepo(active={
        "task_id": "task-1",
        "session_id": "session-1",
        "task_type": "llm_chat",
        "status": "running",
    }))

    result = await ai_service_module.ai_service.get_active_task("project-1", "session-1")

    assert result["active"] is True
    assert result["task_id"] == "task-1"
    assert result["status"] == "running"
    assert result["task_type"] == "llm_chat"
    assert "interaction" not in result


@pytest.mark.asyncio
async def test_get_active_task_includes_interaction_for_parked_row(monkeypatch):
    """An awaiting_input row carries its checkpoint so the frontend can
    restore the interaction modal after a page reload."""
    interaction = {"tool_name": "bash", "interaction_data": {"interaction_type": "permission"}}
    _patch_repo(monkeypatch, FakeTaskStateRepo(active={
        "task_id": "task-1",
        "session_id": "session-1",
        "task_type": "llm_chat",
        "status": "awaiting_input",
        "interaction_state": interaction,
    }))

    result = await ai_service_module.ai_service.get_active_task("project-1", "session-1")

    assert result["active"] is True
    assert result["status"] == "awaiting_input"
    assert result["interaction"]["task_id"] == "task-1"
    assert result["interaction"]["tool_name"] == "bash"


@pytest.mark.asyncio
async def test_get_active_task_reports_idle_when_no_row(monkeypatch):
    _patch_repo(monkeypatch, FakeTaskStateRepo())

    result = await ai_service_module.ai_service.get_active_task("project-1", "session-1")

    assert result == {"active": False, "task_id": None, "status": None}


@pytest.mark.asyncio
async def test_get_active_task_raises_unavailable_when_read_keeps_failing(monkeypatch):
    """A read that keeps failing surfaces as a typed 503 instead of a false
    'no active task': the client must be able to distinguish idle from
    could-not-ask."""
    _patch_repo(monkeypatch, FakeTaskStateRepo(
        read_failures=999, read_error=RuntimeError("database unavailable"),
    ))

    with pytest.raises(TaskStateUnavailableError):
        await ai_service_module.ai_service.get_active_task("project-1", "session-1")


@pytest.mark.asyncio
async def test_get_active_task_retries_transient_read_failure(monkeypatch):
    """One transient read failure is retried once; the retry answers with
    the real row instead of an error."""
    repo = FakeTaskStateRepo(
        active={
            "task_id": "task-1",
            "session_id": "session-1",
            "task_type": "llm_chat",
            "status": "running",
        },
        read_failures=1,
        read_error=OperationalError("stmt", {}, Exception("database is locked")),
    )
    _patch_repo(monkeypatch, repo)

    result = await ai_service_module.ai_service.get_active_task("project-1", "session-1")

    assert repo.get_active_calls == 2
    assert result["active"] is True
    assert result["task_id"] == "task-1"
    assert result["status"] == "running"
