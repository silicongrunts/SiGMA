import asyncio

from app.workers.huey_tasks import _parse_sse_event
from app.workers import huey_tasks
from app.services.library_task_protocol import (
    KIND_DOCUMENT_PROCESS,
    KIND_RAG_INDEX,
    get_task_handler,
)


def test_library_task_handlers_are_registered_by_worker_entrypoint():
    assert get_task_handler(KIND_DOCUMENT_PROCESS) is not None
    assert get_task_handler(KIND_RAG_INDEX) is not None


def test_parse_sse_event_preserves_structured_usage_payload():
    event_type, payload = _parse_sse_event(
        'event: error\n'
        'data: {"error": "provider dropped", '
        '"usage": {"input": 400, "output": 40, "cached": 150}}\n\n'
    )

    assert event_type == "error"
    assert payload == {
        "error": "provider dropped",
        "usage": {"input": 400, "output": 40, "cached": 150},
    }


def test_parse_sse_event_ignores_non_object_payload():
    event_type, payload = _parse_sse_event("event: done\ndata: []\n\n")

    assert event_type == "done"
    assert payload == {}


def test_periodic_library_scan_batches_recovered_work_into_one_wake(monkeypatch):
    wake_calls = []
    scanned = []

    class FakeBackgroundTaskService:
        def wake(self):
            wake_calls.append("wake")

    class FakeDatabaseManager:
        async def cleanup_inactive_projects(self):
            return []

    async def fake_get_db_manager():
        return FakeDatabaseManager()

    async def fake_scan_project(project_id):
        scanned.append(project_id)
        return 74

    def run_now(coro, *, timeout=None):
        return asyncio.run(coro)

    monkeypatch.setattr(huey_tasks, "run_on_worker_loop", run_now)
    monkeypatch.setattr(huey_tasks, "_iter_library_scan_project_ids", lambda: ["p1"])
    monkeypatch.setattr(huey_tasks, "_scan_library_project", fake_scan_project)
    monkeypatch.setattr(
        "app.database.manager.get_db_manager",
        fake_get_db_manager,
    )
    monkeypatch.setattr(
        "app.services.background_task_service.background_task_service",
        FakeBackgroundTaskService(),
    )

    huey_tasks.scan_and_queue_indexing.call_local()

    assert scanned == ["p1"]
    assert wake_calls == ["wake"]
