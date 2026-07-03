import json

import pytest

from app.workers.stream_server import StreamServer, StreamSession, _worker_error_payload
import app.workers.stream_server as stream_server_module


class _FakeUow:
    """Minimal async context manager that exposes a fake task_state repo."""

    def __init__(self, _project_id, task_state):
        self.task_state = task_state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _patch_task_state(monkeypatch, fake_repo):
    """Replace stream_server.UnitOfWork so ``uow.task_state`` is *fake_repo*."""
    monkeypatch.setattr(
        stream_server_module, "UnitOfWork",
        lambda pid: _FakeUow(pid, fake_repo),
    )


class FakeTaskStateRepo:
    def __init__(self):
        self.liveness_calls = 0
        self.mark_failed_calls = []

    async def check_liveness(self, task_id):
        self.liveness_calls += 1
        return "stale" if self.liveness_calls == 1 else "completed"

    async def mark_failed(self, task_id, message):
        self.mark_failed_calls.append((task_id, message))


class AlwaysStaleTaskStateRepo:
    def __init__(self):
        self.mark_failed_calls = []

    async def check_liveness(self, task_id):
        return "stale"

    async def mark_failed(self, task_id, message):
        self.mark_failed_calls.append((task_id, message))


@pytest.mark.asyncio
async def test_subscribe_treats_stale_heartbeat_as_soft_wait(monkeypatch):
    fake_repo = FakeTaskStateRepo()

    _patch_task_state(monkeypatch, fake_repo)
    monkeypatch.setattr(stream_server_module, "STREAM_LIVENESS_POLL_SECONDS", 0.01)

    server = StreamServer()
    server.sessions["task-1"] = StreamSession("task-1", "project-1")

    events = []
    async for event in server.subscribe("task-1", timeout=20):
        events.append(event)
        if event.startswith("event: done"):
            break

    assert events[0].startswith("event: stream_status")
    payload = json.loads(events[0].split("data: ", 1)[1])
    assert payload["status"] == "waiting"
    assert events[-1].startswith("event: done")
    assert fake_repo.mark_failed_calls == []


@pytest.mark.asyncio
async def test_subscribe_fails_when_stale_heartbeat_exceeds_grace(monkeypatch):
    fake_repo = AlwaysStaleTaskStateRepo()

    _patch_task_state(monkeypatch, fake_repo)
    monkeypatch.setattr(stream_server_module, "STREAM_LIVENESS_POLL_SECONDS", 0.01)
    monkeypatch.setattr(stream_server_module, "STREAM_STALE_GRACE_SECONDS", 0.02)

    server = StreamServer()
    server.sessions["task-1"] = StreamSession("task-1", "project-1")

    events = []
    async for event in server.subscribe("task-1", timeout=20):
        events.append(event)
        if event.startswith("event: error"):
            break

    assert events[0].startswith("event: stream_status")
    assert events[-1].startswith("event: error")
    payload = json.loads(events[-1].split("data: ", 1)[1])
    assert "stopped sending heartbeats" in payload["error"]
    assert fake_repo.mark_failed_calls == [("task-1", payload["error"])]


@pytest.mark.asyncio
async def test_worker_error_payload_preserves_usage(monkeypatch):
    fake_repo = AlwaysStaleTaskStateRepo()

    _patch_task_state(monkeypatch, fake_repo)

    server = StreamServer()
    session = StreamSession("task-1", "project-1")
    server.sessions["task-1"] = session

    session.push(
        "event: error\ndata: "
        + json.dumps({
            "error": "provider dropped",
            "usage": {"input": 400, "output": 40, "cached": 150},
        })
        + "\n\n"
    )
    session.done = True

    events = []
    async for event in server.subscribe("task-1", timeout=1):
        events.append(event)
        if event.startswith("event: error"):
            break

    assert len(events) == 1
    payload = json.loads(events[0].split("data: ", 1)[1])
    assert payload == {
        "error": "provider dropped",
        "usage": {"input": 400, "output": 40, "cached": 150},
    }


def test_worker_error_payload_keeps_usage_from_transport_data():
    message, payload = _worker_error_payload({
        "type": "error",
        "message": "provider dropped",
        "data": {
            "error": "provider dropped",
            "usage": {"input": 400, "output": 40, "cached": 150},
        },
    })

    assert message == "provider dropped"
    assert payload == {
        "error": "provider dropped",
        "usage": {"input": 400, "output": 40, "cached": 150},
    }


def test_worker_error_payload_adds_error_to_structured_payload():
    message, payload = _worker_error_payload({
        "type": "error",
        "message": "provider dropped",
        "data": {"usage": {"input": 400, "output": 40, "cached": 150}},
    })

    assert message == "provider dropped"
    assert payload == {
        "error": "provider dropped",
        "usage": {"input": 400, "output": 40, "cached": 150},
    }
