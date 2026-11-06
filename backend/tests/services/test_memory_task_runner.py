"""MemoryTaskRunner: finalize safety, slot lifecycle, and project deletion.

The runner is the only consumer of the memory queue and survives for the
whole process life, so every test here asserts one contract: no outcome of a
single task — failure, cancellation, a deleted project, a broken finalize
write — may kill the loop or wedge the project's single-flight slot forever.
"""

import asyncio

import pytest

from app.core.config import settings
from app.core.task_status import STATUS_QUEUED, STATUS_FAILED
from app.database.unit_of_work import UnitOfWork
from app.services import memory_task_runner as mtr_module
from app.services.background_task_service import background_task_service
from app.services.library_task_protocol import (
    KIND_MEMORY_CONSOLIDATE,
    QUEUE_MEMORY,
)
from app.services.memory_service import MEMORY_CONSOLIDATE_KEY, memory_service
from app.services.memory_task_runner import MemoryTaskRunner


@pytest.fixture(autouse=True)
def _fresh_pending_slot():
    memory_service._pending.clear()
    yield
    memory_service._pending.clear()


def test_runner_singleton_wiring():
    """lifecycle and project_service import the runner by instance name.

    A missing module-level singleton is an ImportError that only fires on a
    real startup — in-process tests never notice, so the seam gets its own
    regression test.
    """
    assert isinstance(mtr_module.memory_task_runner, MemoryTaskRunner)


async def _claim_memory_task(project_id, *, payload=None, max_attempts=3):
    """Enqueue a memory row and claim it the way the runner loop would."""
    await background_task_service.enqueue(
        project_id=project_id,
        kind=KIND_MEMORY_CONSOLIDATE,
        queue=QUEUE_MEMORY,
        dedupe_key=f"test:{project_id}",
        payload=payload or {"task": "consolidate"},
        max_attempts=max_attempts,
    )
    async with UnitOfWork(project_id) as uow:
        return await uow.background_tasks.claim_next(
            queue=QUEUE_MEMORY,
            owner="test-owner",
            lease_seconds=settings.BACKGROUND_TASK_LEASE_SECONDS,
        )


def _use_handler(monkeypatch, handler):
    monkeypatch.setattr(mtr_module, "get_task_handler", lambda kind: handler)


async def test_run_one_survives_handler_failure_and_requeues(project, monkeypatch):
    """A failing handler must not raise past ``_run_one``; a retryable row
    keeps the single-flight slot held (its retry owns the outcome)."""
    ran = []

    async def boom(ctx, payload):
        ran.append(payload)
        raise RuntimeError("handler exploded")

    _use_handler(monkeypatch, boom)
    task = await _claim_memory_task(project)
    memory_service._pending[project] = ("consolidate", "")

    runner = MemoryTaskRunner()
    await runner._run_one(project, task)  # must not raise

    assert ran == [{"task": "consolidate"}]
    assert memory_service._pending[project] == ("consolidate", "")  # slot held for the retry
    async with UnitOfWork(project) as uow:
        row = await uow.background_tasks.get_by_id(task.id)
    assert row.status == STATUS_QUEUED


async def test_run_one_terminal_failure_frees_slot(project, monkeypatch):
    async def boom(ctx, payload):
        raise RuntimeError("handler exploded")

    _use_handler(monkeypatch, boom)
    task = await _claim_memory_task(project, max_attempts=1)
    memory_service._pending[project] = ("consolidate", "")

    await MemoryTaskRunner()._run_one(project, task)

    assert project not in memory_service._pending
    async with UnitOfWork(project) as uow:
        row = await uow.background_tasks.get_by_id(task.id)
    assert row.status == STATUS_FAILED


async def test_run_one_skips_finalize_for_deleted_project(project, monkeypatch):
    """A project deleted mid-run has no database to finalize into — the slot
    frees anyway and the runner survives."""
    monkeypatch.setattr(mtr_module, "project_allows_persistence", lambda pid: False)
    ran = []

    async def ok(ctx, payload):
        ran.append(payload)

    _use_handler(monkeypatch, ok)
    task = await _claim_memory_task(project, payload={"task": "extract", "session_id": "s1"})
    memory_service._pending[project] = ("extract", "s1")

    await MemoryTaskRunner()._run_one(project, task)  # must not raise

    assert ran == [{"task": "extract", "session_id": "s1"}]
    assert project not in memory_service._pending


async def test_run_one_survives_broken_finalize_write(project, monkeypatch):
    """A finalize write that itself fails must not kill the runner; the row
    is left to its lease-expiry reclaim and the slot stays held."""

    async def ok(ctx, payload):
        return None

    _use_handler(monkeypatch, ok)

    class BrokenUow:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError("database gone")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(mtr_module, "UnitOfWork", BrokenUow)
    task = await _claim_memory_task(project)
    memory_service._pending[project] = ("consolidate", "")

    await MemoryTaskRunner()._run_one(project, task)  # must not raise

    assert memory_service._pending[project] == ("consolidate", "")  # retry via lease expiry


async def test_run_one_cancels_heartbeat_task(project, monkeypatch):
    """The heartbeat task must die with its task, not linger to its next
    20-second tick after the row is already finalized."""
    heartbeat_cancelled = asyncio.Event()
    original = MemoryTaskRunner._heartbeat_while_running

    async def spy(self, ctx):
        try:
            await original(self, ctx)
        except asyncio.CancelledError:
            heartbeat_cancelled.set()
            raise

    monkeypatch.setattr(MemoryTaskRunner, "_heartbeat_while_running", spy)

    async def ok(ctx, payload):
        await asyncio.sleep(0)  # yield once so the heartbeat task starts
        return None

    _use_handler(monkeypatch, ok)
    task = await _claim_memory_task(project)

    await MemoryTaskRunner()._run_one(project, task)

    assert heartbeat_cancelled.is_set()


async def test_runner_survives_handler_cancellation_and_drains(project, monkeypatch):
    """End-to-end single-flight loop: a handler stopped by a project-level
    cancel finalizes its row, frees the slot, and — unlike a shutdown
    cancellation — leaves the runner alive for the next task."""
    started = asyncio.Event()
    saw_cancel = asyncio.Event()

    async def handler(ctx, payload):
        started.set()
        while not ctx.cancel_event.is_set():
            await asyncio.sleep(0.01)
        saw_cancel.set()
        raise asyncio.CancelledError()  # the documented handler contract

    _use_handler(monkeypatch, handler)
    assert await memory_service.start_due_task(project, "consolidate") is True

    runner = MemoryTaskRunner()
    try:
        runner.start()
        await asyncio.wait_for(started.wait(), timeout=5)
        assert runner._active_project == project

        # The real drain flow flags the DB row before signalling the
        # handler (cancel_all_active: RUNNING → CANCELLING), so the cancel
        # confirmation can land at finalize.
        async with UnitOfWork(project) as uow:
            await uow.background_tasks.cancel_all_active()

        runner.cancel_project(project)
        assert await runner.wait_for_project(project, timeout=5) is True
        assert saw_cancel.is_set()

        # wait_for_project covers finalize, so the slot is deterministically
        # free once it returns — no sleep-and-hope.
        assert not runner._task.done()  # the loop survived the cancellation
        assert project not in memory_service._pending
    finally:
        await runner.stop()


async def test_unflagged_cancel_that_requeues_holds_slot(project, monkeypatch):
    """A handler cancelled without the DB CANCELLING flag (a shutdown stop,
    a spontaneous CancelledError) falls back to failed-or-retry: a
    scheduled retry owns the outcome and keeps the slot held, exactly like
    the failure branch."""
    async def ok(ctx, payload):
        raise asyncio.CancelledError()

    _use_handler(monkeypatch, ok)
    task = await _claim_memory_task(project)
    memory_service._pending[project] = ("consolidate", "")

    runner = MemoryTaskRunner()
    with pytest.raises(asyncio.CancelledError):
        await runner._run_one(project, task)

    assert memory_service._pending[project] == ("consolidate", "")  # the retry owns the slot
    async with UnitOfWork(project) as uow:
        row = await uow.background_tasks.get_by_id(task.id)
    assert row.status == STATUS_QUEUED


async def test_wait_for_project_times_out_and_signals(project):
    """A handler that ignores the cancel event makes wait_for_project give
    up (False) after signalling — project deletion then blocks loudly."""
    runner = MemoryTaskRunner()
    runner._active_project = project
    stubborn = asyncio.Event()
    runner._cancel_events_by_project.setdefault(project, set()).add(stubborn)

    async def wait_short():
        return await runner.wait_for_project(project, timeout=0.1)

    assert await asyncio.wait_for(wait_short(), timeout=2) is False
    assert stubborn.is_set()  # the timeout path signalled the handler

    runner._active_project = None
    runner._cancel_events_by_project.clear()


async def test_run_never_runs_handler_for_failed_claim(project, monkeypatch):
    """A row claim_next already wrote off (repeated lease expiry) must never
    run its handler — the claim is terminal, so only the slot is freed."""
    from datetime import timedelta

    from app.core.utils import utcnow

    called = []

    async def must_not_run(ctx, payload):
        called.append(payload)

    _use_handler(monkeypatch, must_not_run)
    task = await _claim_memory_task(project, max_attempts=1)
    async with UnitOfWork(project) as uow:
        row = await uow.background_tasks.get_by_id(task.id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        await uow.commit()

    async with UnitOfWork(project) as uow:
        await uow.config.set(MEMORY_CONSOLIDATE_KEY, "0")
    memory_service._pending[project] = ("consolidate", "")

    mtr_module.note_memory_project(project)
    runner = MemoryTaskRunner()
    try:
        runner.start()
        deadline = asyncio.get_running_loop().time() + 5
        while project in memory_service._pending:
            assert asyncio.get_running_loop().time() < deadline, "slot never freed"
            await asyncio.sleep(0.01)
    finally:
        await runner.stop()

    assert called == []
    async with UnitOfWork(project) as uow:
        value = await uow.config.get(MEMORY_CONSOLIDATE_KEY, "")
    assert value == str(settings.memory.consolidation_interval)
    async with UnitOfWork(project) as uow:
        row = await uow.background_tasks.get_by_id(task.id)
    assert row.status == STATUS_FAILED
