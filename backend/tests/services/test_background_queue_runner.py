"""Library background queue runner and maintenance sweep tests."""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.document_status import STATUS_FAILED, STATUS_INDEXING
from app.core.task_status import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED as TASK_FAILED,
    STATUS_QUEUED,
)
from app.database.repos.background_task_repo import BackgroundTaskRepository
from app.database.repos.library_repo import LibraryRepository
from app.services import background_task_service as bts


async def _wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


async def _run_until(runner, predicate, monkeypatch=None):
    """Drive ``runner.run()`` on a task until *predicate* holds, then stop it.

    ``run`` never returns on its own (it idles on the wake event between
    passes), so the test cancels it once the observation is complete.
    """
    loop_task = asyncio.create_task(runner.run())
    bts.background_task_service.wake_event.set()
    await _wait_until(predicate)
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)


def _real_uow_factory(db_session_factory):
    class RealUow:
        def __init__(self, _project_id, **_kwargs):
            self._session = None

        async def __aenter__(self):
            self._session = db_session_factory()
            self.background_tasks = BackgroundTaskRepository(self._session)
            self.library = LibraryRepository(self._session)
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            await self._session.close()

    return RealUow


def _make_tasks(count):
    return [
        SimpleNamespace(
            id=f"task-{idx}", kind="test", queue=bts.QUEUE_LIBRARY,
            payload_json="{}",
        )
        for idx in range(count)
    ]


# ---------------------------------------------------------------------------
# Runner loop scheduling
# ---------------------------------------------------------------------------


async def test_queue_runner_never_exceeds_configured_concurrency(monkeypatch):
    runner = bts.LibraryTaskRunner()
    tasks = _make_tasks(5)
    running = 0
    max_running = 0
    processed = []

    monkeypatch.setattr(bts.settings.background, "library_concurrency", 2)

    async def fake_claim_next():
        await asyncio.sleep(0)
        return ("p1", tasks.pop(0)) if tasks else None

    async def fake_run_one(_project_id, task):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        running -= 1
        processed.append(task.id)

    monkeypatch.setattr(runner, "_claim_next", fake_claim_next)
    monkeypatch.setattr(runner, "_run_one", fake_run_one)

    await _run_until(runner, lambda: len(processed) == 5)

    assert max_running == 2
    assert sorted(processed) == [f"task-{idx}" for idx in range(5)]


async def test_queue_runner_wakes_itself_after_batch_budget_exhaustion(monkeypatch):
    """When the batch budget fills with work likely remaining, the runner
    self-wakes and keeps claiming in the same run instead of idling."""
    runner = bts.LibraryTaskRunner()
    tasks = _make_tasks(5)
    processed = []
    wakes = []
    real_wake = bts.background_task_service.wake

    def recording_wake():
        wakes.append("wake")
        real_wake()

    monkeypatch.setattr(bts.settings.background, "library_concurrency", 2)
    monkeypatch.setattr(bts.settings.background, "library_queue_batch_size", 3)
    monkeypatch.setattr(bts.background_task_service, "wake", recording_wake)

    async def fake_claim_next():
        await asyncio.sleep(0)
        return ("p1", tasks.pop(0)) if tasks else None

    async def fake_run_one(_project_id, task):
        await asyncio.sleep(0)
        processed.append(task.id)

    monkeypatch.setattr(runner, "_claim_next", fake_claim_next)
    monkeypatch.setattr(runner, "_run_one", fake_run_one)

    await _run_until(runner, lambda: len(processed) == 5)

    assert len(wakes) >= 1  # the batch-exhaustion wake beyond the initial kick


async def test_library_runner_start_is_idempotent_and_stop_cancels(monkeypatch):
    monkeypatch.setattr(bts, "iter_project_ids", lambda: iter([]))
    wakes = []
    monkeypatch.setattr(bts.background_task_service, "wake", lambda: wakes.append("wake"))
    runner = bts.LibraryTaskRunner()

    runner.start()
    first = runner._runner_task
    runner.start()
    assert runner._runner_task is first
    assert wakes == ["wake"]

    await runner.stop()
    assert first.done()
    assert runner._runner_task is None

    await runner.stop()  # stopping an already-stopped runner is a no-op


async def test_runner_processes_existing_queued_work_in_real_repo(db_session_factory, monkeypatch):
    monkeypatch.setattr(bts, "UnitOfWork", _real_uow_factory(db_session_factory))
    processed = asyncio.Event()

    async def handler(_ctx, _payload):
        processed.set()

    monkeypatch.setattr(bts, "get_task_handler", lambda _kind: handler)
    service = bts.BackgroundTaskService()
    task_id = await service.enqueue(
        project_id="p1",
        kind="test",
        payload={},
        max_attempts=1,
        wake=False,
    )
    runner = bts.LibraryTaskRunner()
    monkeypatch.setattr(bts, "iter_project_ids", lambda: iter(["p1"]))
    runner.start()
    try:
        await asyncio.wait_for(processed.wait(), timeout=2)
        for _ in range(100):
            async with bts.UnitOfWork("p1") as uow:
                task = await uow.background_tasks.get_by_id(task_id)
            if task.status == STATUS_COMPLETED:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(f"queued task did not complete: {task.status}")
    finally:
        await runner.stop()


@pytest.mark.parametrize("max_attempts, expected_status", [(1, TASK_FAILED), (2, STATUS_QUEUED)])
async def test_handler_failure_updates_real_task_and_document_rows(
    db_session_factory, monkeypatch, max_attempts, expected_status,
):
    monkeypatch.setattr(bts, "UnitOfWork", _real_uow_factory(db_session_factory))
    async def failing_handler(_ctx, _payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(bts, "get_task_handler", lambda _kind: failing_handler)
    async with bts.UnitOfWork("p1") as uow:
        doc = await uow.library.create(
            title="Queued document", processing_status=STATUS_INDEXING,
        )
        task = await uow.background_tasks.enqueue(
            kind=bts.KIND_DOCUMENT_PROCESS,
            queue=bts.QUEUE_LIBRARY,
            payload_json=f'{{"doc_id": "{doc.id}", "doc_revision": {doc.revision}}}',
            max_attempts=max_attempts,
            dedupe_key=f"{bts.KIND_DOCUMENT_PROCESS}:p1:{doc.id}:{doc.revision}",
        )

    runner = bts.LibraryTaskRunner()
    async with bts.UnitOfWork("p1") as uow:
        claimed_task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY,
            owner=runner.owner,
            lease_seconds=600,
        )
    assert claimed_task.id == task.id
    await runner._run_one("p1", claimed_task)

    async with bts.UnitOfWork("p1") as uow:
        stored_task = await uow.background_tasks.get_by_id(task.id)
        stored_doc = await uow.library.get_by_id(doc.id)
    assert stored_task.status == expected_status
    assert stored_task.attempt_count == 1
    if expected_status == TASK_FAILED:
        assert stored_doc.processing_status == STATUS_FAILED
    else:
        assert stored_doc.processing_status == STATUS_INDEXING


async def test_handler_failure_respects_real_cancellation_state(db_session_factory, monkeypatch):
    monkeypatch.setattr(bts, "UnitOfWork", _real_uow_factory(db_session_factory))
    async def cancelling_handler(_ctx, _payload):
        await bts.background_task_service.cancel_document_tasks("p1", doc.id)
        raise RuntimeError("boom")

    monkeypatch.setattr(bts, "get_task_handler", lambda _kind: cancelling_handler)
    async with bts.UnitOfWork("p1") as uow:
        doc = await uow.library.create(
            title="Cancelled document", processing_status=STATUS_INDEXING,
        )
        task = await uow.background_tasks.enqueue(
            kind=bts.KIND_DOCUMENT_PROCESS,
            queue=bts.QUEUE_LIBRARY,
            payload_json=f'{{"doc_id": "{doc.id}", "doc_revision": {doc.revision}}}',
            max_attempts=1,
            dedupe_key=f"{bts.KIND_DOCUMENT_PROCESS}:p1:{doc.id}:{doc.revision}",
        )

    runner = bts.LibraryTaskRunner()
    async with bts.UnitOfWork("p1") as uow:
        claimed_task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY,
            owner=runner.owner,
            lease_seconds=600,
        )
    await runner._run_one("p1", claimed_task)

    async with bts.UnitOfWork("p1") as uow:
        stored_task = await uow.background_tasks.get_by_id(task.id)
        stored_doc = await uow.library.get_by_id(doc.id)
    assert stored_task.status == STATUS_CANCELLED
    assert stored_doc.processing_status == STATUS_INDEXING


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_enqueue_uow_factory(*, enqueued_ids):
    class FakeTask:
        def __init__(self, task_id):
            self.id = task_id

    class FakeBackgroundTasks:
        async def enqueue(self, **_kwargs):
            return FakeTask(enqueued_ids.pop(0))

    class FakeUow:
        def __init__(self, _project_id, **_kwargs):
            self.background_tasks = FakeBackgroundTasks()

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    return FakeUow


async def test_enqueue_can_skip_wake(monkeypatch):
    """Enqueue paths that batch their own wake (maintenance recovery) must be
    able to enqueue without ending the runner's idle wait."""
    service = bts.BackgroundTaskService()
    wake_calls = []

    monkeypatch.setattr(service, "wake", lambda: wake_calls.append("wake"))
    monkeypatch.setattr(
        bts,
        "UnitOfWork",
        _fake_enqueue_uow_factory(enqueued_ids=["task-1"]),
    )

    task_id = await service.enqueue(
        project_id="p1",
        kind=bts.KIND_DOCUMENT_PROCESS,
        payload={"doc_id": "doc-1"},
        wake=False,
    )

    assert task_id == "task-1"
    assert wake_calls == []


# ---------------------------------------------------------------------------
# Maintenance sweep: recovery of interrupted library work
# ---------------------------------------------------------------------------


class _FakeDatabaseManager:
    async def cleanup_inactive_projects(self):
        return []


def _patch_sweep(monkeypatch, *, project_ids, scan, database_manager=None):
    async def fake_get_db_manager():
        return database_manager or _FakeDatabaseManager()

    monkeypatch.setattr(bts, "iter_project_ids", lambda: iter(project_ids))
    monkeypatch.setattr(
        "app.database.manager.get_db_manager", fake_get_db_manager,
    )
    monkeypatch.setattr(bts.background_task_service, "_scan_library_project", scan)


async def test_maintenance_sweep_scans_every_project_and_wakes_runner(monkeypatch):
    scanned = []
    wakes = []

    async def fake_scan(project_id):
        scanned.append(project_id)
        return 2

    _patch_sweep(monkeypatch, project_ids=["p1", "p2"], scan=fake_scan)
    monkeypatch.setattr(
        bts.background_task_service, "wake", lambda: wakes.append("wake"),
    )

    await bts.background_task_service._maintenance_sweep()

    assert scanned == ["p1", "p2"]
    assert wakes == ["wake"]  # recovered work starts without an external event


async def test_maintenance_sweep_continues_after_project_scan_failure(monkeypatch):
    scanned = []
    wakes = []

    async def fake_scan(project_id):
        scanned.append(project_id)
        if project_id == "bad":
            raise RuntimeError("database unavailable")
        return 0

    _patch_sweep(monkeypatch, project_ids=["bad", "good"], scan=fake_scan)
    monkeypatch.setattr(
        bts.background_task_service, "wake", lambda: wakes.append("wake"),
    )

    await bts.background_task_service._maintenance_sweep()

    assert scanned == ["bad", "good"]
    assert wakes == ["wake"]


async def test_maintenance_sweep_isolates_per_project_timeouts(monkeypatch):
    """One project's scan overrunning its timeout must not stall the sweep or
    suppress the runner wake for the remaining projects."""
    scanned = []
    wakes = []

    async def slow_scan(project_id):
        scanned.append(project_id)
        await asyncio.sleep(0.3)

    monkeypatch.setattr(
        bts.settings.background, "library_scan_project_timeout_seconds", 0.05,
    )
    _patch_sweep(monkeypatch, project_ids=["slow", "next"], scan=slow_scan)
    monkeypatch.setattr(
        bts.background_task_service, "wake", lambda: wakes.append("wake"),
    )

    await asyncio.wait_for(
        bts.background_task_service._maintenance_sweep(), timeout=5,
    )

    assert scanned == ["slow", "next"]
    assert wakes == ["wake"]


async def test_maintenance_loop_start_is_idempotent_and_stop_cancels(monkeypatch):
    monkeypatch.setattr(bts, "iter_project_ids", lambda: iter([]))
    service = bts.background_task_service

    service.start_maintenance()
    first = service._maintenance_task
    service.start_maintenance()
    assert service._maintenance_task is first

    await service.stop_maintenance()
    assert first.done()
    assert service._maintenance_task is None


async def test_daily_cleanup_round_is_bounded_by_total_timeout(monkeypatch):
    """A hung cleanup round (the Chroma chunk scan it contains can stall)
    must be cut at the configured budget so the maintenance loop reaches its
    next recovery sweep instead of stalling behind the cleanup."""
    monkeypatch.setattr(bts, "_MAINTENANCE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(
        bts.settings.background, "library_scan_total_timeout_seconds", 0.05,
    )

    service = bts.BackgroundTaskService()
    sweeps = []

    async def counting_sweep():
        sweeps.append("sweep")

    async def hung_cleanup():
        await asyncio.sleep(30)  # far longer than the budget

    monkeypatch.setattr(service, "_maintenance_sweep", counting_sweep)
    monkeypatch.setattr(service, "_cleanup_all_projects", hung_cleanup)

    loop_task = asyncio.create_task(service._maintenance_loop())
    try:
        await _wait_until(lambda: len(sweeps) >= 2, timeout=5)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    # The second sweep proves the timed-out first cleanup round did not
    # take the maintenance loop (and its recovery sweeps) down with it.
    assert sweeps == ["sweep", "sweep"]


# ---------------------------------------------------------------------------
# Lease ownership: the claim token, and loss reaching the handler
# ---------------------------------------------------------------------------


async def test_heartbeat_ownership_loss_signals_handler_cancellation(monkeypatch):
    """When the heartbeat finds the lease re-claimed (stale claim token),
    the loss must reach the executing handler: the context's cancel event
    is set and is_cancelling() turns true at the next checkpoint."""
    runner = bts.LibraryTaskRunner()
    ctx = bts.RunningTaskContext(
        project_id="p1", task_id="task-1",
        owner="host:claim-token", lease_seconds=600,
    )

    async def lost_heartbeat():
        return False

    monkeypatch.setattr(ctx, "heartbeat", lost_heartbeat)
    monkeypatch.setattr(runner, "_heartbeat_interval", lambda _lease: 0.01)

    heartbeat_task = asyncio.create_task(runner._heartbeat_while_running(ctx))
    try:
        await _wait_until(lambda: ctx.cancel_event.is_set())
        assert await ctx.is_cancelling() is True
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def test_run_one_uses_claim_token_for_heartbeat_and_finalize(monkeypatch):
    """_run_one must carry the one-time claim token into the handler context
    and every finalize call, never the runner's own identity."""
    runner = bts.LibraryTaskRunner()
    task = SimpleNamespace(
        id="task-1",
        kind="test",
        payload_json="{}",
        lease_owner="host:claim-token",
    )
    seen = []

    async def handler(ctx, _payload):
        seen.append(("handler", ctx.owner))

    class FakeBackgroundTasks:
        async def is_cancelling(self, _task_id):
            return False

        async def mark_completed(self, _task_id, owner):
            seen.append(("completed", owner))

        async def mark_failed_or_retry(self, _task_id, owner, _error):
            seen.append(("retry", owner))
            return "failed"

    class FakeUow:
        def __init__(self, _project_id, **_kwargs):
            self.background_tasks = FakeBackgroundTasks()

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(bts, "get_task_handler", lambda _kind: handler)
    monkeypatch.setattr(bts, "UnitOfWork", FakeUow)
    monkeypatch.setattr(runner, "_heartbeat_interval", lambda _lease: 3600)

    await runner._run_one("p1", task)

    assert seen == [
        ("handler", "host:claim-token"),
        ("completed", "host:claim-token"),
    ]


# ---------------------------------------------------------------------------
# Wake event: per-running-loop rebinding and pre-start wakes
# ---------------------------------------------------------------------------


async def test_wake_event_rebinds_when_the_running_loop_changes():
    """A fresh loop wakes its own waiter without reviving the old event."""
    service = bts.BackgroundTaskService()
    first = service.wake_event
    old_waiter = asyncio.create_task(first.wait())
    await asyncio.sleep(0)

    async def wait_on_new_loop():
        service.wake()
        await asyncio.wait_for(service.wake_event.wait(), timeout=1)
        return service.wake_event

    second = await asyncio.to_thread(asyncio.run, wait_on_new_loop())

    assert second is not first
    assert not old_waiter.done()
    old_waiter.cancel()
    await asyncio.gather(old_waiter, return_exceptions=True)


async def test_wake_before_runner_start_is_not_lost(monkeypatch):
    """A wake issued before the runner starts waiting — an enqueue racing
    runner startup — must end the runner's first idle wait immediately."""
    runner = bts.LibraryTaskRunner()
    claims = []

    async def fake_claim_next():
        claims.append("claim")
        return None

    monkeypatch.setattr(runner, "_claim_next", fake_claim_next)

    bts.background_task_service.wake()
    loop_task = asyncio.create_task(runner.run())
    try:
        await _wait_until(lambda: len(claims) >= 1, timeout=2)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Project lifecycle barrier
# ---------------------------------------------------------------------------


async def test_project_barrier_blocks_post_delete_persistence(monkeypatch):
    monkeypatch.setattr(bts, "get_project_status", lambda _project_id: "deleting")

    assert bts._project_allows_persistence("p1") is False
