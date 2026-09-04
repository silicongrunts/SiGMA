"""Annotation reply submission arbitration.

Annotation task_state rows carry session_id=NULL and an annotation owner id:
their claim, launch-failure finalization, terminal-row pruning, and deletion
drain are the annotation-side mirror of the chat session-claim contract.
"""

import asyncio

import pytest

from app.core.config import settings
from app.core.exceptions import TaskActiveError
from app.core.task_status import STATUS_RUNNING
from app.core.utils import generate_id
from app.database.unit_of_work import UnitOfWork
from app.services import task_runtime
from app.services.file_service import file_service
from tests.ai.matrix_harness import use_fixture_project_root  # noqa: F401 (autouse fixture)


# ---------------------------------------------------------------------------
# Annotation reply submission arbitration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotation_double_submit_raises_task_active_not_500(
    project, monkeypatch,
):
    """The partial unique index rejects a concurrent second submit for the
    same annotation; the conflict surfaces as TaskActiveError (409), not an
    unhandled IntegrityError (500)."""
    from app.services.annotation_service import annotation_service

    monkeypatch.setattr(task_runtime, "launch", lambda **kw: None)

    task_id, _ = await annotation_service.start_ai_reply_stream(
        project, file_path="notes.md", annotation_id="anno-1",
    )
    assert task_id

    with pytest.raises(TaskActiveError):
        await annotation_service.start_ai_reply_stream(
            project, file_path="notes.md", annotation_id="anno-1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("launch_error,expected_error_part", [
    (RuntimeError("no runner available"), "Failed to start annotation task"),
    (asyncio.CancelledError(), "cancelled before it started"),
])
async def test_annotation_launch_failure_finalizes_queued_row(
    project, monkeypatch, launch_error, expected_error_part,
):
    """A failure between the queued-row claim and the runner launch —
    including CancelledError from a disconnecting client — must finalize the
    row as failed before propagating, instead of stranding a queued row that
    only the 75s sweep would reclaim."""
    from app.services.annotation_service import annotation_service

    from sqlalchemy import select

    from app.database.models import TaskState

    def failing_launch(**_kwargs):
        raise launch_error

    monkeypatch.setattr(task_runtime, "launch", failing_launch)

    with pytest.raises(type(launch_error)):
        await annotation_service.start_ai_reply_stream(
            project, file_path="notes.md", annotation_id="anno-fail",
        )

    async with UnitOfWork(project) as uow:
        row = (await uow.session.execute(
            select(TaskState).where(TaskState.owner_id == "anno-fail")
        )).scalar_one()
    assert row.status == "failed"
    assert expected_error_part in row.error


@pytest.mark.asyncio
async def test_annotation_reply_prunes_own_terminal_task_state_rows(
    project, monkeypatch,
):
    """Annotation replies prune their own terminal task_state rows: the rows
    carry session_id=NULL, so the chat-side session prune never reaches them
    and each submit must trim its owner's terminal history instead."""
    from sqlalchemy import select

    from app.database.models import TaskState
    from app.services.annotation_service import annotation_service

    monkeypatch.setattr(task_runtime, "launch", lambda **kw: None)

    for _ in range(8):
        task_id, _ = await annotation_service.start_ai_reply_stream(
            project, file_path="notes.md", annotation_id="anno-1",
        )
        async with UnitOfWork(project) as uow:
            await uow.task_state.mark_completed(task_id)

    async with UnitOfWork(project) as uow:
        rows = list((await uow.session.execute(
            select(TaskState).where(TaskState.owner_id == "anno-1")
        )).scalars())
    terminal = [r for r in rows if r.status != "awaiting_input"]
    # keep=5: each submit trims its owner's terminal rows to the five most
    # recent, then inserts its own queued row — after eight completed
    # submissions exactly six rows remain, all terminal.
    assert len(rows) == 6 and len(terminal) == 6
    assert all(r.status == "completed" for r in rows)


@pytest.mark.asyncio
async def test_delete_annotation_removes_task_state_rows(project, monkeypatch):
    """Deleting an annotation also removes its task_state rows, which no
    foreign key cascade covers."""
    from sqlalchemy import select

    from app.database.models import TaskState
    from app.services.annotation_service import annotation_service

    monkeypatch.setattr(task_runtime, "launch", lambda **kw: None)
    settings.get_project_path(project).joinpath("notes.md").write_text("text")

    async with UnitOfWork(project) as uow:
        await uow.annotations.apply_mutation_cas(
            "notes.md",
            [{"id": "anno-del", "from": 0, "to": 5, "originalText": "text"}],
            [],
            0,
            file_service.compute_hash("text"),
        )
    _task_id, _ = await annotation_service.start_ai_reply_stream(
        project, file_path="notes.md", annotation_id="anno-del",
    )
    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(_task_id)
        await uow.task_state.mark_cancelled(_task_id)
    result = await annotation_service.delete_annotation(project, "anno-del")
    assert result["deleted"] is True

    async with UnitOfWork(project) as uow:
        rows = list((await uow.session.execute(
            select(TaskState).where(TaskState.owner_id == "anno-del")
        )).scalars())
    assert rows == []


@pytest.mark.asyncio
async def test_delete_running_annotation_cancels_and_drains_runner(project):
    from app.services.annotation_service import annotation_service

    annotation_id = "anno-running-delete"
    settings.get_project_path(project).joinpath("notes.md").write_text("text")
    async with UnitOfWork(project) as uow:
        await uow.annotations.apply_mutation_cas(
            "notes.md",
            [{"id": annotation_id, "from": 0, "to": 5, "originalText": "text"}],
            [],
            0,
            file_service.compute_hash("text"),
        )
        task_id = generate_id()
        await uow.task_state.set_queued(
            task_id, owner_type="annotation", owner_id=annotation_id,
        )

    async def source(cancel_event):
        await cancel_event.wait()
        if False:
            yield {}

    task_runtime.launch(
        task_id=task_id, project_id=project, source_factory=source,
    )
    for _ in range(100):
        async with UnitOfWork(project) as uow:
            row = await uow.task_state.get_by_id(task_id)
        if row["status"] == STATUS_RUNNING:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("annotation runner did not start")

    result = await annotation_service.delete_annotation(project, annotation_id)
    assert result["deleted"] is True
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_by_id(annotation_id) is None
        assert await uow.task_state.get_by_id(task_id) is None


@pytest.mark.asyncio
async def test_delete_parked_annotation_clears_checkpoint_and_task_state(project):
    from app.services.annotation_service import annotation_service

    annotation_id = "anno-parked-delete"
    task_id = generate_id()
    settings.get_project_path(project).joinpath("notes.md").write_text("text")
    async with UnitOfWork(project) as uow:
        await uow.annotations.apply_mutation_cas(
            "notes.md",
            [{"id": annotation_id, "from": 0, "to": 5, "originalText": "text"}],
            [],
            0,
            file_service.compute_hash("text"),
        )
        await uow.task_state.set_queued(
            task_id, owner_type="annotation", owner_id=annotation_id,
        )
        await uow.task_state.mark_awaiting_input(
            task_id, {"tool_name": "ask_user_question"},
        )

    result = await annotation_service.delete_annotation(project, annotation_id)
    assert result["deleted"] is True
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_by_id(annotation_id) is None
        assert await uow.task_state.get_by_id(task_id) is None
