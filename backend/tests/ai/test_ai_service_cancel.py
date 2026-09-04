"""ai_service.cancel_task: truthful status reporting, runner signaling, and
finalization of stranded rows against fake task_state and runner boundaries."""

import pytest
from sqlalchemy.exc import OperationalError

import app.services.ai_service as ai_service_module
import app.services.task_runtime as task_runtime_module
from tests.ai.conftest import FakeTaskStateRepo, make_fake_uow


class _FakeCancel:
    """Records task_runtime.cancel signals and simulates runner liveness.

    (Stays local: it fakes the in-process runner registry, not the DB.)"""

    def __init__(self, runner_alive=True):
        self.runner_alive = runner_alive
        self.cancel_calls = []

    def __call__(self, task_id):
        self.cancel_calls.append(task_id)
        return self.runner_alive


def _patch(monkeypatch, repo, runner_alive=True):
    fake_cancel = _FakeCancel(runner_alive)
    monkeypatch.setattr(
        ai_service_module, "UnitOfWork", make_fake_uow(task_state=repo),
    )
    monkeypatch.setattr(task_runtime_module, "cancel", fake_cancel)
    return fake_cancel


@pytest.mark.asyncio
async def test_cancel_task_returns_truthful_result_for_cancelling(monkeypatch):
    repo = FakeTaskStateRepo(cancel_status="cancelling")
    fake_cancel = _patch(monkeypatch, repo)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelling", "task_id": "task-1"}
    assert repo.requested == ["task-1"]
    assert fake_cancel.cancel_calls == ["task-1"]  # runner signal still issued


@pytest.mark.asyncio
async def test_cancel_task_reports_cancelled_for_parked_awaiting_input(monkeypatch):
    """Cancelling an awaiting_input task finalizes straight to cancelled."""
    _patch(monkeypatch, FakeTaskStateRepo(cancel_status="cancelled"))

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelled", "task_id": "task-1"}


@pytest.mark.asyncio
async def test_cancel_task_reports_not_cancelled_for_terminal(monkeypatch):
    _patch(monkeypatch, FakeTaskStateRepo(cancel_status="completed"))

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": False, "status": "completed", "task_id": "task-1"}


@pytest.mark.asyncio
async def test_cancel_task_reports_not_cancelled_for_missing(monkeypatch):
    _patch(monkeypatch, FakeTaskStateRepo(cancel_status="not_found"))

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": False, "status": "not_found", "task_id": "task-1"}


@pytest.mark.asyncio
async def test_cancel_task_not_found_does_not_signal_runner(monkeypatch):
    """A task id absent from the project's database — never existed here, or
    belongs to another project — must not reach any in-process runner: the
    cancel signal is gated on the database confirming the task belongs to
    this project, so a cross-project cancel cannot wind down someone else's
    task while the API reports not_found."""
    repo = FakeTaskStateRepo(cancel_status="not_found")
    fake_cancel = _patch(monkeypatch, repo)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": False, "status": "not_found", "task_id": "task-1"}
    assert fake_cancel.cancel_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
async def test_cancel_task_without_runner_finalizes_active_row(monkeypatch, status):
    """With no live runner the cancel path must finalize the stranded row
    itself: every active status request_cancel can leave behind is marked
    cancelled in the database and reported as cancelled."""
    repo = FakeTaskStateRepo(
        cancel_status=status, row={"task_id": "task-1", "status": "cancelled"},
    )
    fake_cancel = _patch(monkeypatch, repo, runner_alive=False)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelled", "task_id": "task-1"}
    assert repo.requested == ["task-1"]
    assert repo.mark_cancelled_calls == ["task-1"]
    assert repo.get_by_id_calls == ["task-1"]
    assert fake_cancel.cancel_calls == ["task-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["cancelled", "completed", "failed", "not_found"],
)
async def test_cancel_task_without_runner_spares_final_row(monkeypatch, status):
    """A row request_cancel already left terminal (or never found) needs no
    finalize: without a runner the path must not write again and must report
    the recorded status unchanged."""
    repo = FakeTaskStateRepo(cancel_status=status)
    _patch(monkeypatch, repo, runner_alive=False)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {
        "cancelled": status == "cancelled",
        "status": status,
        "task_id": "task-1",
    }
    assert repo.mark_cancelled_calls == []
    assert repo.get_by_id_calls == []


@pytest.mark.asyncio
async def test_cancel_task_without_runner_reports_request_status_when_row_vanishes(
    monkeypatch,
):
    """If the stranded row disappears between mark_cancelled and the re-read,
    the path falls back to the status request_cancel recorded instead of
    crashing or inventing a terminal one."""
    repo = FakeTaskStateRepo(cancel_status="cancelling", row=None)
    _patch(monkeypatch, repo, runner_alive=False)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelling", "task_id": "task-1"}
    assert repo.mark_cancelled_calls == ["task-1"]


@pytest.mark.asyncio
async def test_cancel_task_surfaces_database_failure_without_signaling_runner(monkeypatch):
    """A durable cancel that never lands must surface the database error, not
    report success, and must not signal the runner to wind down."""
    repo = FakeTaskStateRepo(
        cancel_failures=999,
        cancel_error=OperationalError(
            "UPDATE task_state", {}, Exception("database is locked"),
        ),
    )
    fake_cancel = _patch(monkeypatch, repo)

    with pytest.raises(OperationalError):
        await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert fake_cancel.cancel_calls == []


@pytest.mark.asyncio
async def test_cancel_task_retries_on_locked_db(monkeypatch):
    """A transient locked-DB OperationalError must be retried, not surfaced as
    a 500, because the compare-and-swap cancel is idempotent."""
    repo = FakeTaskStateRepo(
        cancel_status="cancelling",
        cancel_failures=1,
        cancel_error=OperationalError(
            "UPDATE task_state", {}, Exception("database is locked"),
        ),
    )
    fake_cancel = _patch(monkeypatch, repo)

    result = await ai_service_module.ai_service.cancel_task("project-1", "task-1")

    assert result == {"cancelled": True, "status": "cancelling", "task_id": "task-1"}
    assert repo.request_cancel_calls == 2  # first attempt locked, second succeeded
    assert fake_cancel.cancel_calls == ["task-1"]
