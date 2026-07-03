import asyncio
from types import SimpleNamespace

import pytest

from app.services import background_task_service as bts


@pytest.mark.asyncio
async def test_queue_runner_respects_configured_concurrency(monkeypatch):
    runner = bts.LibraryTaskRunner()
    tasks = [
        SimpleNamespace(id=f"task-{idx}", project_id="p1", kind="test", queue=bts.QUEUE_LIBRARY)
        for idx in range(5)
    ]
    running = 0
    max_running = 0

    monkeypatch.setattr(bts.settings.workers, "library_workers", 2)

    async def fake_claim_next():
        await asyncio.sleep(0)
        return tasks.pop(0) if tasks else None

    async def fake_run_one(task):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.01)
        running -= 1

    monkeypatch.setattr(runner, "_claim_next", fake_claim_next)
    monkeypatch.setattr(runner, "_run_one", fake_run_one)

    result = await runner.run()

    assert result == {"queue": bts.QUEUE_LIBRARY, "processed": 5, "status": "done"}
    assert max_running == 2


@pytest.mark.asyncio
async def test_queue_runner_yields_after_batch_limit(monkeypatch):
    runner = bts.LibraryTaskRunner()
    tasks = [
        SimpleNamespace(id=f"task-{idx}", project_id="p1", kind="test", queue=bts.QUEUE_LIBRARY)
        for idx in range(5)
    ]
    wake_calls = []

    monkeypatch.setattr(bts.settings.workers, "library_workers", 2)
    monkeypatch.setattr(bts.settings.workers, "library_queue_batch_size", 3)
    monkeypatch.setattr(bts.background_task_service, "wake", lambda: wake_calls.append("wake"))

    async def fake_claim_next():
        await asyncio.sleep(0)
        return tasks.pop(0) if tasks else None

    async def fake_run_one(task):
        await asyncio.sleep(0)

    monkeypatch.setattr(runner, "_claim_next", fake_claim_next)
    monkeypatch.setattr(runner, "_run_one", fake_run_one)

    result = await runner.run()

    assert result == {"queue": bts.QUEUE_LIBRARY, "processed": 3, "status": "yielded"}
    assert len(tasks) == 2
    assert wake_calls == ["wake"]


@pytest.mark.asyncio
async def test_document_process_final_failure_marks_current_document_failed(monkeypatch):
    runner = bts.LibraryTaskRunner()
    task = SimpleNamespace(
        id="task-1",
        project_id="p1",
        kind=bts.KIND_DOCUMENT_PROCESS,
        payload_json='{"doc_id": "doc-1", "doc_revision": 7}',
    )
    failed_docs = []

    async def failing_handler(_ctx, _payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(bts, "get_task_handler", lambda _kind: failing_handler)
    monkeypatch.setattr(
        bts,
        "UnitOfWork",
        _fake_uow_factory(
            mark_failed_status="failed",
            doc=SimpleNamespace(id="doc-1", revision=7),
            failed_docs=failed_docs,
        ),
    )

    await runner._run_one(task)

    assert failed_docs == [
        ("doc-1", "Document processing failed after task retries: boom")
    ]


@pytest.mark.asyncio
async def test_document_process_final_failure_does_not_mark_newer_revision(monkeypatch):
    runner = bts.LibraryTaskRunner()
    task = SimpleNamespace(
        id="task-1",
        project_id="p1",
        kind=bts.KIND_DOCUMENT_PROCESS,
        payload_json='{"doc_id": "doc-1", "doc_revision": 7}',
    )
    failed_docs = []

    async def failing_handler(_ctx, _payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(bts, "get_task_handler", lambda _kind: failing_handler)
    monkeypatch.setattr(
        bts,
        "UnitOfWork",
        _fake_uow_factory(
            mark_failed_status="failed",
            doc=SimpleNamespace(id="doc-1", revision=8),
            failed_docs=failed_docs,
        ),
    )

    await runner._run_one(task)

    assert failed_docs == []


@pytest.mark.asyncio
async def test_lease_expired_final_failure_marks_current_document_failed(monkeypatch):
    runner = bts.LibraryTaskRunner()
    task = SimpleNamespace(
        id="task-1",
        project_id="p1",
        kind=bts.KIND_DOCUMENT_PROCESS,
        payload_json='{"doc_id": "doc-1", "doc_revision": 7}',
    )
    failed_docs = []

    monkeypatch.setattr(
        bts,
        "UnitOfWork",
        _fake_uow_factory(
            mark_failed_status="failed",
            doc=SimpleNamespace(id="doc-1", revision=7),
            failed_docs=failed_docs,
        ),
    )

    await runner._apply_final_failure(
        task,
        "Task lease expired too many times; marking failed",
    )

    assert failed_docs == [
        (
            "doc-1",
            "Document processing failed after task retries: "
            "Task lease expired too many times; marking failed",
        )
    ]


@pytest.mark.asyncio
async def test_enqueue_can_skip_huey_wake(monkeypatch):
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


def _fake_uow_factory(*, mark_failed_status, doc, failed_docs):
    class FakeBackgroundTasks:
        async def mark_failed_or_retry(self, _task_id, _owner, _error):
            return mark_failed_status

    class FakeLibrary:
        async def get_by_id(self, _doc_id):
            return doc

        async def mark_failed(self, doc_id, message):
            failed_docs.append((doc_id, message))

    class FakeUow:
        def __init__(self, _project_id):
            self.background_tasks = FakeBackgroundTasks()
            self.library = FakeLibrary()

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    return FakeUow


def _fake_enqueue_uow_factory(*, enqueued_ids):
    class FakeTask:
        def __init__(self, task_id):
            self.id = task_id

    class FakeBackgroundTasks:
        async def enqueue(self, **_kwargs):
            return FakeTask(enqueued_ids.pop(0))

    class FakeUow:
        def __init__(self, _project_id):
            self.background_tasks = FakeBackgroundTasks()

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    return FakeUow
