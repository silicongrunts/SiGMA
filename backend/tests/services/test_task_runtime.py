"""TaskRunner and task_runtime module contract tests.

Drives the real runner against a real per-project SQLite database
(Alembic-migrated); only the event source is scripted. Each test pins one
branch of the runner's state machine: what the durable ``task_state`` row
says afterwards and which terminal frames a subscriber saw — the two views
that must agree after a refresh.
"""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

import app.core.config as config_module
from app.core.chat_events import make_event
from app.core.utils import utcnow
from app.core import project_registry
from app.database.manager import get_db_manager
from app.database.models import TaskState
from app.database.repos.task_state_repo import TaskStateRepository
from app.database.unit_of_work import UnitOfWork
from app.services import task_runtime
from app.services.ai_service import ai_service
from app.services.stream_hub import StreamSession, stream_hub
from app.services.task_runtime import TaskRunner

CANCELLED_PAYLOAD = {"message": "Task cancelled by user"}
RESTART_ERROR = "Task was interrupted by an application restart."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def make_queued(project, session_id=None):
    """Insert a queued task row with its own session."""
    task_id = f"task-{uuid4().hex[:12]}"
    session_id = session_id or f"session-{uuid4().hex[:8]}"
    async with UnitOfWork(project) as uow:
        await uow.task_state.set_queued(
            task_id, session_id=session_id,
            owner_type="chat_session", owner_id=session_id,
        )
    return task_id


async def get_row(project, task_id):
    async with UnitOfWork(project) as uow:
        return await uow.task_state.get_by_id(task_id)


def parse_frame(frame):
    """Split one SSE frame into ``(seq, event_type, data_dict)``."""
    lines = frame.split("\n")
    assert lines[0].startswith("id: "), frame
    assert lines[1].startswith("event: "), frame
    assert lines[2].startswith("data: "), frame
    assert lines[3] == "" and lines[4] == "", f"malformed frame: {frame!r}"
    return (
        int(lines[0][len("id: "):]),
        lines[1][len("event: "):],
        json.loads(lines[2][len("data: "):]),
    )


def scripted_source(events, *, after=None):
    """A source factory yielding *events*; ``after`` hooks post-yield work."""
    def factory(cancel_event):
        async def gen():
            for event in events:
                yield event
            if after is not None:
                await after(cancel_event)
        return gen()
    return factory


async def run_task(project, task_id, source_factory):
    """Run one TaskRunner to completion, returning (frames, session)."""
    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id,
        project_id=project,
        source_factory=source_factory,
        session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    await asyncio.wait_for(runner.run(), timeout=10)
    await asyncio.wait_for(collector, timeout=5)
    return frames, session


async def wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


async def wait_until_status(project, task_id, status, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not await _status_is(project, task_id, status):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"task {task_id} never reached status {status!r}"
            )
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def second_project(project, monkeypatch):
    """A second independent active project under the same userdata root.

    Mirrors the ``project`` fixture's setup on the already-monkeypatched
    root, so tests can exercise cross-project task id lookups against two
    real per-project databases.
    """
    pid = f"proj{uuid4().hex[:20]}"
    root = config_module.USERDATA_DIR
    (root / pid / ".SiGMA").mkdir(parents=True)
    registry_path = root / ".SiGMA" / "projects.json"
    entries = json.loads(registry_path.read_text())
    entries[pid] = {"status": "active", "name": "Other", "description": ""}
    registry_path.write_text(json.dumps(entries))
    monkeypatch.setattr(project_registry, "_registry_cache", None)
    monkeypatch.setattr(project_registry, "_registry_mtime", 0.0)

    manager = await get_db_manager()
    await manager.ensure_db_exists(pid)
    yield pid
    await manager.cleanup_project(pid)


# ---------------------------------------------------------------------------
# Pre-flight guards
# ---------------------------------------------------------------------------


async def test_runner_fails_task_when_project_is_inactive(project, monkeypatch):
    monkeypatch.setattr(task_runtime, "is_project_active", lambda pid: False)
    task_id = await make_queued(project)

    async def factory(cancel_event):
        raise AssertionError("source must not start for an inactive project")
        yield  # pragma: no cover - makes this an async generator

    frames, _ = await run_task(project, task_id, factory)

    row = await get_row(project, task_id)
    assert row["status"] == "failed"
    assert row["error"] == "Project is inactive."
    assert [parse_frame(f)[1:] for f in frames] == [
        ("error", {"error": "Project is inactive."}),
    ]
    assert stream_hub.get(task_id) is None


async def test_runner_finalizes_task_cancelled_before_start(project):
    """A cancel winning the race before the run starts must end as cancelled
    without the source ever executing."""
    task_id = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(task_id)

    async def factory(cancel_event):
        raise AssertionError("source must not start for a cancelling task")
        yield  # pragma: no cover

    frames, _ = await run_task(project, task_id, factory)

    assert (await get_row(project, task_id))["status"] == "cancelled"
    assert [parse_frame(f)[1:] for f in frames] == [("cancelled", CANCELLED_PAYLOAD)]


async def _finalize_completed(repo, task_id):
    await repo.mark_completed(task_id)


async def _finalize_failed(repo, task_id):
    await repo.mark_failed(task_id, "earlier boom")


async def _finalize_cancelled(repo, task_id):
    await repo.request_cancel(task_id)
    await repo.mark_cancelled(task_id)


@pytest.mark.parametrize("finalize,expected_status,terminal_type,terminal_data", [
    (_finalize_completed, "completed", "done", {}),
    (_finalize_failed, "failed", "error", {"error": "Task failed"}),
    (_finalize_cancelled, "cancelled", "cancelled", CANCELLED_PAYLOAD),
])
async def test_runner_renders_terminal_frame_without_rerunning_terminal_task(
    project, finalize, expected_status, terminal_type, terminal_data,
):
    """A task already terminal in the DB gets its terminal frame rendered and
    the source is never started."""
    task_id = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await finalize(uow.task_state, task_id)

    async def factory(cancel_event):
        raise AssertionError("source must not start for a terminal task")
        yield  # pragma: no cover

    frames, _ = await run_task(project, task_id, factory)

    assert (await get_row(project, task_id))["status"] == expected_status
    assert [parse_frame(f)[1:] for f in frames] == [
        (terminal_type, terminal_data),
    ]


@pytest.mark.parametrize("park", [False, True])
async def test_runner_rejects_task_it_cannot_start_without_clobbering_row(
    project, park,
):
    """A task with no state row, or one parked for input that no runner may
    resume in place, gets a not-found error frame on the stream — and the
    guarded failure write leaves the parked checkpoint (or absent row)
    untouched instead of inventing or clobbering durable state."""
    if park:
        task_id = await make_queued(project)
        async with UnitOfWork(project) as uow:
            await uow.task_state.mark_awaiting_input(
                task_id, {"tool_name": "bash"},
            )
    else:
        task_id = f"task-{uuid4().hex[:12]}"  # no row at all

    async def factory(cancel_event):
        raise AssertionError("source must not start without a queued row")
        yield  # pragma: no cover

    frames, _ = await run_task(project, task_id, factory)

    assert [parse_frame(f)[1:] for f in frames] == [
        ("error", {"error": "Task state record not found."}),
    ]
    row = await get_row(project, task_id)
    if park:
        assert row["status"] == "awaiting_input"
    else:
        assert row is None


# ---------------------------------------------------------------------------
# Streaming and finalization
# ---------------------------------------------------------------------------


async def test_runner_promotes_queued_task_and_streams_source_events(project):
    task_id = await make_queued(project)
    captured = {}

    def factory(cancel_event):
        captured["cancel_event"] = cancel_event

        async def gen():
            yield make_event("delta", {"content": "a"})
            yield make_event("delta", {"content": "b"})
        return gen()

    frames, session = await run_task(project, task_id, factory)

    assert captured["cancel_event"] is session.cancel_event
    assert [parse_frame(f) for f in frames] == [
        (1, "delta", {"content": "a"}),
        (2, "delta", {"content": "b"}),
        (3, "done", {}),
    ]
    assert (await get_row(project, task_id))["status"] == "completed"


async def test_runner_parks_for_input_and_leaves_status_untouched(project):
    """When the loop parks the task for user input, the checkpoint row must
    survive untouched and the done frame only closes the stream."""
    task_id = await make_queued(project)

    async def park(cancel_event):
        async with UnitOfWork(project) as uow:
            await uow.task_state.mark_awaiting_input(task_id, {
                "tool_name": "bash",
                "interaction_data": {"interaction_type": "permission"},
            })

    source = scripted_source([make_event("delta", {"content": "x"})], after=park)

    frames, _ = await run_task(project, task_id, source)

    row = await get_row(project, task_id)
    assert row["status"] == "awaiting_input"
    assert row["interaction_state"]["tool_name"] == "bash"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("done", {}),
    ]


async def test_runner_finalizes_cancel_that_lands_mid_run(project):
    """A durable cancel recorded while the source runs winds the task down to
    cancelled with exactly one cancelled frame."""
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await cancel_event.wait()
        return gen()

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=factory, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "running")

    # The cancel handler's order: durable intent first, runner signal second.
    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(task_id)
    assert task_runtime.cancel(task_id) is True

    await asyncio.wait_for(handle, timeout=5)
    await asyncio.wait_for(collector, timeout=5)

    assert (await get_row(project, task_id))["status"] == "cancelled"
    terminal = [parse_frame(f) for f in frames if parse_frame(f)[1] == "cancelled"]
    assert len(terminal) == 1
    assert terminal[0][2] == CANCELLED_PAYLOAD


async def _status_is(project, task_id, status):
    row = await get_row(project, task_id)
    return row is not None and row["status"] == status


async def test_runner_marks_failed_when_source_emits_error(project):
    task_id = await make_queued(project)
    source = scripted_source([
        make_event("delta", {"content": "partial"}),
        make_event("error", {"error": "provider dropped"}),
    ])

    frames, _ = await run_task(project, task_id, source)

    row = await get_row(project, task_id)
    assert row["status"] == "failed"
    assert row["error"] == "provider dropped"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "partial"}),
        ("error", {"error": "provider dropped"}),
    ]


async def test_cancel_recorded_during_the_completion_write_cannot_undo_it(
    project, monkeypatch,
):
    """A cancel recorded while the completion write is in flight does not
    change the outcome: the finalize-phase decision is completed, the row
    ends completed, and the subscriber sees the done frame — the two views
    must agree."""
    original = TaskStateRepository.mark_completed

    async def cancel_in_window(self, task_id):
        await self.request_cancel(task_id)
        await original(self, task_id)

    monkeypatch.setattr(TaskStateRepository, "mark_completed", cancel_in_window)
    task_id = await make_queued(project)
    source = scripted_source([make_event("delta", {"content": "x"})])

    frames, _ = await run_task(project, task_id, source)

    assert (await get_row(project, task_id))["status"] == "completed"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("done", {}),
    ]


async def test_cancel_signalled_mid_run_with_delivered_done_ends_completed(
    project,
):
    """The budget-exhausted shape: the cancel event fires mid-run, but the
    source still ends with a done frame because the reply completed within
    budget. The delivered done is the real outcome: the row ends completed
    and the subscriber sees done — never a cancelled frame after a finished
    reply."""
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await cancel_event.wait()
            yield make_event("done", {})
        return gen()

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=factory, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "running")

    # The cancel handler's order: durable intent first, runner signal second.
    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(task_id)
    assert task_runtime.cancel(task_id) is True

    await asyncio.wait_for(handle, timeout=5)
    await asyncio.wait_for(collector, timeout=5)

    assert (await get_row(project, task_id))["status"] == "completed"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("done", {}),
    ]


async def test_runner_pushes_exactly_one_done_when_source_already_emitted_done(project):
    task_id = await make_queued(project)
    source = scripted_source([make_event("done", {})])

    frames, _ = await run_task(project, task_id, source)

    assert [parse_frame(f)[1:] for f in frames] == [("done", {})]
    assert (await get_row(project, task_id))["status"] == "completed"


# ---------------------------------------------------------------------------
# Finalize write resilience
# ---------------------------------------------------------------------------


def _locked_db_error():
    return OperationalError(
        "UPDATE task_state", {}, Exception("database is locked"),
    )


async def test_safe_mark_retries_transient_finalize_failures(project, monkeypatch):
    """Transient locked-DB finalize writes are retried until they land, so a
    momentary SQLite contention cannot strand the row in a runnable status."""
    monkeypatch.setattr(task_runtime, "FINALIZE_RETRY_DELAY_SECONDS", 0.01)
    original = TaskStateRepository.mark_completed
    attempts = {"count": 0}

    async def flaky(self, task_id):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise _locked_db_error()
        await original(self, task_id)

    monkeypatch.setattr(TaskStateRepository, "mark_completed", flaky)
    task_id = await make_queued(project)
    source = scripted_source([make_event("delta", {"content": "x"})])

    frames, _ = await run_task(project, task_id, source)

    assert attempts["count"] == 3
    assert (await get_row(project, task_id))["status"] == "completed"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("done", {}),
    ]


async def test_post_stream_read_failure_still_delivers_done(project, monkeypatch):
    """A database read failing after the source finished cleanly must not
    turn the stream into an error: the subscriber gets its done frame and
    the finalization lands through the contained fallback path."""
    original_get_status = TaskStateRepository.get_status
    reads = {"count": 0}

    async def flaky_get_status(self, task_id):
        reads["count"] += 1
        if reads["count"] == 2:  # the post-loop read; the first is pre-flight
            raise _locked_db_error()
        return await original_get_status(self, task_id)

    monkeypatch.setattr(TaskStateRepository, "get_status", flaky_get_status)
    task_id = await make_queued(project)
    source = scripted_source([make_event("delta", {"content": "x"})])

    frames, _ = await run_task(project, task_id, source)

    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("done", {}),
    ]
    assert (await get_row(project, task_id))["status"] == "completed"


async def test_second_cancel_cannot_abort_the_cancel_finalization(
    project, monkeypatch,
):
    """A second cancel (the shutdown grace window expiring) lands while the
    CancelledError handler's finalize write is in flight; the shielded
    finalization must still land so the row is not stranded in a runnable
    status."""
    finalize_started = asyncio.Event()
    release = asyncio.Event()
    original_mark_cancelled = TaskStateRepository.mark_cancelled

    async def slow_mark_cancelled(self, task_id):
        finalize_started.set()
        await release.wait()
        await original_mark_cancelled(self, task_id)

    monkeypatch.setattr(
        TaskStateRepository, "mark_cancelled", slow_mark_cancelled,
    )
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await asyncio.sleep(3600)
        return gen()

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=factory, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "running")

    handle.cancel()
    await asyncio.wait_for(finalize_started.wait(), timeout=3)
    handle.cancel()  # second cancel mid-write
    release.set()  # the detached shielded finalize proceeds
    results = await asyncio.gather(handle, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    await asyncio.wait_for(collector, timeout=5)
    # The raced decision cannot know the outcome yet, so the stream closes
    # with the state-neutral done frame while the shielded finalize lands.
    assert parse_frame(frames[-1])[1:] == ("done", {})
    await wait_until_status(project, task_id, "cancelled")


async def test_hard_cancel_during_completion_write_keeps_delivered_done_completed(
    project, monkeypatch,
):
    """A hard cancel (shutdown grace expiry) injected while the post-stream
    completion write is in flight must not overturn a delivered done: the
    CancelledError handler's finalize converges on completed and no frame
    follows the terminal one."""
    finalize_started = asyncio.Event()
    release = asyncio.Event()
    original = TaskStateRepository.mark_completed

    async def slow_mark_completed(self, task_id):
        finalize_started.set()
        await release.wait()
        await original(self, task_id)

    monkeypatch.setattr(TaskStateRepository, "mark_completed", slow_mark_completed)
    task_id = await make_queued(project)
    source = scripted_source([
        make_event("delta", {"content": "x"}),
        make_event("done", {}),
    ])

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=source, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "running")
    await asyncio.wait_for(finalize_started.wait(), timeout=3)

    handle.cancel()  # hard cancel lands mid-completion-write
    release.set()  # let the aborted write's replacement proceed
    results = await asyncio.gather(handle, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    await asyncio.wait_for(collector, timeout=5)
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("done", {}),
    ]
    assert (await get_row(project, task_id))["status"] == "completed"


async def test_cancel_finalizes_row_stranded_by_dead_runner(project, monkeypatch):
    """A runner whose finalize write never lands still releases its subscriber
    with a terminal frame, and the cancel path then finalizes the stranded row
    directly so the session is free to submit again."""
    monkeypatch.setattr(task_runtime, "FINALIZE_RETRY_DELAY_SECONDS", 0.01)

    async def always_locked(self, task_id):
        raise _locked_db_error()

    monkeypatch.setattr(TaskStateRepository, "mark_completed", always_locked)
    session_id = f"session-{uuid4().hex[:8]}"
    task_id = await make_queued(project, session_id)
    source = scripted_source([make_event("delta", {"content": "x"})])

    frames, _ = await run_task(project, task_id, source)

    # The subscriber is released even though the durable row stayed runnable.
    assert parse_frame(frames[-1])[1:] == ("done", {})
    assert (await get_row(project, task_id))["status"] == "running"
    assert stream_hub.get(task_id) is None

    result = await ai_service.cancel_task(project, task_id)

    assert result == {"cancelled": True, "status": "cancelled", "task_id": task_id}
    assert (await get_row(project, task_id))["status"] == "cancelled"

    # The submit gate reads the same durable row: no active task remains.
    async with UnitOfWork(project) as uow:
        assert await uow.task_state.get_active_by_session(session_id) is None


async def test_cancel_racing_queued_claim_never_starts_source(project, monkeypatch):
    """The real queued CAS decides whether the source owns execution."""
    task_id = await make_queued(project)
    claim_entered = asyncio.Event()
    release_claim = asyncio.Event()
    source_started = False
    original_mark_running = TaskStateRepository.mark_running

    async def delayed_mark_running(repo, candidate_id):
        claim_entered.set()
        await release_claim.wait()
        return await original_mark_running(repo, candidate_id)

    monkeypatch.setattr(TaskStateRepository, "mark_running", delayed_mark_running)

    async def source(_cancel_event):
        nonlocal source_started
        source_started = True
        yield make_event("done", {})

    runner_task = asyncio.create_task(run_task(project, task_id, source))
    await asyncio.wait_for(claim_entered.wait(), timeout=3)
    async with UnitOfWork(project) as uow:
        assert await uow.task_state.request_cancel(task_id) == "cancelling"
    release_claim.set()
    frames, _ = await asyncio.wait_for(runner_task, timeout=5)

    assert source_started is False
    assert (await get_row(project, task_id))["status"] == "cancelled"
    assert [parse_frame(frame)[1:] for frame in frames] == [
        ("cancelled", CANCELLED_PAYLOAD),
    ]

async def test_sse_listen_renders_done_for_parked_task_without_session(project):
    """A reconnecting subscriber to a parked task gets the same done frame the
    runner pushes when parking, not a false "not active" error."""
    task_id = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_awaiting_input(task_id, {"tool_name": "bash"})

    frames = [frame async for frame in ai_service.sse_listen(task_id, project_id=project)]

    assert frames[0] == f"event: task_id\ndata: {{\"task_id\": \"{task_id}\"}}\n\n"
    assert frames[1] == 'event: done\ndata: {}\n\n'
    assert len(frames) == 2


@pytest.mark.parametrize("status", ["queued", "running", "cancelling"])
async def test_sse_listen_ends_without_terminal_for_runnable_row_without_session(
    project, status,
):
    """A runnable row with no live session sits in the legal claim→launch
    window (message persistence and the project touch run between the claim
    commit and the runner launch), or awaits the stranded-row sweep: no
    terminal frame, so a reconnecting second tab retries instead of ending
    its turn on a false terminal."""
    task_id = await make_queued(project)
    if status != "queued":
        async with UnitOfWork(project) as uow:
            if status == "running":
                await uow.task_state.mark_running(task_id)
            else:
                await uow.task_state.request_cancel(task_id)

    frames = [frame async for frame in ai_service.sse_listen(task_id, project_id=project)]

    assert frames == [f"event: task_id\ndata: {{\"task_id\": \"{task_id}\"}}\n\n"]


async def test_sse_listen_yields_task_id_frame_then_replay_after_cursor(
    project, monkeypatch,
):
    """A reconnecting client receives the unseq'd task_id bootstrap frame
    first, then exactly the buffered events after its cursor — the live
    session composition the SSE route delivers, not just the no-session one."""
    monkeypatch.setattr(StreamSession, "KEEPALIVE_INTERVAL_SECONDS", 0.01)
    task_id = await make_queued(project)
    session = stream_hub.create(task_id, project)
    session.push(make_event("delta", {"content": "a"}))
    session.push(make_event("delta", {"content": "b"}))
    session.finish()

    frames = [
        frame async for frame in ai_service.sse_listen(
            task_id, cursor=1, project_id=project,
        )
    ]
    stream_hub.remove(task_id, session)

    assert frames[0] == f"event: task_id\ndata: {{\"task_id\": \"{task_id}\"}}\n\n"
    assert frames[1:] == ['id: 2\nevent: delta\ndata: {"content": "b"}\n\n']


async def test_sse_listen_renders_not_active_error_when_row_is_absent(project):
    """A task with no live session and no durable row never ran here: the
    not-active error terminal is the honest close for the stream."""
    task_id = f"task-{uuid4().hex[:12]}"

    frames = [frame async for frame in ai_service.sse_listen(task_id, project_id=project)]

    assert frames[0] == f"event: task_id\ndata: {{\"task_id\": \"{task_id}\"}}\n\n"
    assert frames[1:] == ['event: error\ndata: {"error": "Task is not active."}\n\n']


async def test_sse_listen_treats_cross_project_session_as_unknown(
    project, second_project,
):
    """A live session of another project's task must not be reachable: the
    subscription answers exactly as it would for a task id the requesting
    project has never heard of, and no foreign frame is forwarded."""
    task_id = await make_queued(project)
    session = stream_hub.create(task_id, project)
    session.push(make_event("delta", {"content": "secret"}))

    try:
        frames = [
            frame async for frame in ai_service.sse_listen(
                task_id, project_id=second_project,
            )
        ]
    finally:
        stream_hub.remove(task_id, session)

    assert frames[0] == f"event: task_id\ndata: {{\"task_id\": \"{task_id}\"}}\n\n"
    assert frames[1:] == ['event: error\ndata: {"error": "Task is not active."}\n\n']


async def test_cancel_task_cross_project_reports_not_found_and_spares_session(
    project, second_project,
):
    """Cancelling another project's task id answers exactly as a task this
    project does not know: the other project's live session gets no cancel
    signal and its durable row is never written."""
    task_id = await make_queued(project)
    session = stream_hub.create(task_id, project)

    try:
        result = await ai_service.cancel_task(second_project, task_id)

        assert result == {
            "cancelled": False, "status": "not_found", "task_id": task_id,
        }
        assert not session.cancel_event.is_set()
        assert (await get_row(project, task_id))["status"] == "queued"
    finally:
        stream_hub.remove(task_id, session)


async def test_cancel_task_signals_live_session_of_same_project(project):
    """The ownership gate must not disturb the normal path: cancelling a
    task of the requesting project still sets its session's cancel event and
    records the cancelling status."""
    task_id = await make_queued(project)
    session = stream_hub.create(task_id, project)

    try:
        result = await ai_service.cancel_task(project, task_id)

        assert result == {
            "cancelled": True, "status": "cancelling", "task_id": task_id,
        }
        assert session.cancel_event.is_set()
        assert (await get_row(project, task_id))["status"] == "cancelling"
    finally:
        stream_hub.remove(task_id, session)


async def test_sse_listen_ends_without_terminal_when_row_read_keeps_failing(
    project, monkeypatch,
):
    """A row read that keeps failing is transient contention, not evidence
    of absence: once the bounded retries are exhausted the stream ends with
    no terminal frame, so the client's reconnect logic retries instead of
    stopping on a false "Task is not active." terminal."""
    monkeypatch.setattr("app.services.ai_service.RETRY_DELAY", 0.001)

    async def locked(self, task_id):
        raise _locked_db_error()

    monkeypatch.setattr(TaskStateRepository, "get_by_id", locked)
    task_id = await make_queued(project)

    frames = [frame async for frame in ai_service.sse_listen(task_id, project_id=project)]

    assert frames == [f"event: task_id\ndata: {{\"task_id\": \"{task_id}\"}}\n\n"]


async def test_sse_listen_retries_transient_row_read_failure_then_renders_row(
    project, monkeypatch,
):
    """A transient locked-database row read is retried: once it lands, the
    terminal comes from the durable row — here a failed task's recorded
    error — never from the read failure itself."""
    monkeypatch.setattr("app.services.ai_service.RETRY_DELAY", 0.001)
    original = TaskStateRepository.get_by_id
    reads = {"count": 0}

    async def flaky(self, task_id):
        reads["count"] += 1
        if reads["count"] == 1:
            raise _locked_db_error()
        return await original(self, task_id)

    monkeypatch.setattr(TaskStateRepository, "get_by_id", flaky)
    task_id = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_failed(task_id, "boom")

    frames = [frame async for frame in ai_service.sse_listen(task_id, project_id=project)]

    assert reads["count"] == 2
    assert frames[0] == f"event: task_id\ndata: {{\"task_id\": \"{task_id}\"}}\n\n"
    assert frames[1:] == ['event: error\ndata: {"error": "boom"}\n\n']


async def test_cancel_outranks_source_exception(project):
    """A source raising while a cancel lands finalizes as cancelled: the
    user's intent outranks the wind-down error, and the subscriber sees the
    cancelled frame instead of an error."""
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await cancel_event.wait()
            raise RuntimeError("wind-down failure")
        return gen()

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=factory, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "running")

    # The cancel handler's order: durable intent first, runner signal second.
    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(task_id)
    assert task_runtime.cancel(task_id) is True

    await asyncio.wait_for(handle, timeout=5)
    await asyncio.wait_for(collector, timeout=5)

    assert (await get_row(project, task_id))["status"] == "cancelled"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("cancelled", CANCELLED_PAYLOAD),
    ]


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


async def test_runner_marks_failed_when_source_raises(project):
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "partial"})
            raise RuntimeError("boom")
        return gen()

    frames, _ = await run_task(project, task_id, factory)

    row = await get_row(project, task_id)
    assert row["status"] == "failed"
    assert row["error"] == "boom"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "partial"}),
        ("error", {"error": "boom"}),
    ]


async def test_runner_does_not_duplicate_error_frame_after_source_error(project):
    """A source that already delivered its error frame and then raises must
    not produce a second error frame for the subscriber."""
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("error", {"error": "provider dropped"})
            raise ValueError("late wind-down failure")
        return gen()

    frames, _ = await run_task(project, task_id, factory)

    row = await get_row(project, task_id)
    assert row["status"] == "failed"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("error", {"error": "provider dropped"}),
    ]


async def test_hard_cancel_finalizes_running_task_as_cancelled(project):
    """A hard cancellation (process shutdown) surfaces the cancelled frame,
    re-raises, and finalizes the durable row as cancelled: the subscriber saw
    cancellation, so the row must not stay runnable waiting for the next
    startup reconciliation."""
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await asyncio.sleep(3600)
        return gen()

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=factory, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "running")

    handle.cancel()
    results = await asyncio.gather(handle, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    await asyncio.wait_for(collector, timeout=5)
    assert parse_frame(frames[-1])[1:] == ("cancelled", CANCELLED_PAYLOAD)
    assert (await get_row(project, task_id))["status"] == "cancelled"
    assert stream_hub.get(task_id) is None
    # Nothing left for the next startup's reconciliation to recover.
    assert await task_runtime.startup_reconcile() == 0


async def test_hard_cancel_during_error_winddown_still_finalizes_row(
    project, monkeypatch,
):
    """A hard cancel landing inside the source-exception wind-down — while the
    mark_failed write is blocked — must go through the same shielded
    finalization as a cancel landing mid-flight: the row must not stay
    running with no runner, and the stream gets exactly one terminal
    consistent with the row."""
    winddown_started = asyncio.Event()
    release = asyncio.Event()
    original = TaskStateRepository.mark_failed

    async def blocked_mark_failed(self, task_id, message):
        winddown_started.set()
        await release.wait()
        await original(self, task_id, message)

    monkeypatch.setattr(TaskStateRepository, "mark_failed", blocked_mark_failed)
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            raise RuntimeError("boom")
        return gen()

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=factory, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "running")
    await asyncio.wait_for(winddown_started.wait(), timeout=3)

    handle.cancel()  # hard cancel lands inside the blocked mark_failed write
    release.set()  # let the aborted write's surroundings drain
    results = await asyncio.gather(handle, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    await asyncio.wait_for(collector, timeout=5)

    # mark_failed never landed, so the shielded finalize read the row as
    # running and recorded the cancel: the subscriber's cancelled frame and
    # the durable row agree.
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("cancelled", CANCELLED_PAYLOAD),
    ]
    assert (await get_row(project, task_id))["status"] == "cancelled"
    assert stream_hub.get(task_id) is None
    assert await task_runtime.startup_reconcile() == 0


async def test_hard_cancel_of_parked_task_pushes_done_and_keeps_checkpoint(project):
    """A hard cancellation landing on a parked run must not contradict the
    row: the checkpoint survives untouched, so the stream closes with the
    same done frame the park path uses — never a cancelled frame that would
    mark the round interrupted while the row still waits for input."""
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            async with UnitOfWork(project) as uow:
                await uow.task_state.mark_awaiting_input(
                    task_id, {"tool_name": "bash"},
                )
            await asyncio.sleep(3600)
            yield make_event("delta", {"content": "x"})  # pragma: no cover
        return gen()

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=factory, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "awaiting_input")

    handle.cancel()
    results = await asyncio.gather(handle, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    await asyncio.wait_for(collector, timeout=5)

    row = await get_row(project, task_id)
    assert row["status"] == "awaiting_input"
    assert row["interaction_state"]["tool_name"] == "bash"
    assert [parse_frame(f)[1:] for f in frames] == [("done", {})]


async def test_second_cancel_race_on_parked_row_pushes_state_neutral_done(
    project, monkeypatch,
):
    """A second cancel aborting the frame decision while the shielded
    finalize's status read is still blocked leaves the outcome genuinely
    unknown when the frame is picked. The pushed frame must not contradict
    the row however the finalize lands: here the parked checkpoint survives,
    so the stream closes with the state-neutral done frame — a cancelled
    frame would hide the live interaction behind a dead stream."""
    original_get_status = TaskStateRepository.get_status
    reads = {"count": 0}
    status_read_blocked = asyncio.Event()
    release_status_read = asyncio.Event()

    async def gated_get_status(self, task_id):
        reads["count"] += 1
        if reads["count"] == 1:  # the pre-flight read
            return await original_get_status(self, task_id)
        status_read_blocked.set()
        await release_status_read.wait()
        return await original_get_status(self, task_id)

    monkeypatch.setattr(TaskStateRepository, "get_status", gated_get_status)
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            async with UnitOfWork(project) as uow:
                await uow.task_state.mark_awaiting_input(
                    task_id, {"tool_name": "bash"},
                )
            await asyncio.sleep(3600)
            yield make_event("delta", {"content": "x"})  # pragma: no cover
        return gen()

    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id, project_id=project,
        source_factory=factory, session=session,
    )
    frames = []

    async def collect():
        async for frame in session.subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())
    handle = asyncio.create_task(runner.run())
    await wait_until_status(project, task_id, "awaiting_input")

    handle.cancel()  # first hard cancel: the shielded finalize starts
    await asyncio.wait_for(status_read_blocked.wait(), timeout=3)
    handle.cancel()  # second cancel aborts the frame decision, not the finalize
    results = await asyncio.gather(handle, return_exceptions=True)

    assert isinstance(results[0], asyncio.CancelledError)
    release_status_read.set()  # the resumed finalize reads parked, pushes nothing
    await asyncio.wait_for(collector, timeout=5)

    row = await get_row(project, task_id)
    assert row["status"] == "awaiting_input"
    assert row["interaction_state"]["tool_name"] == "bash"
    assert [parse_frame(f)[1:] for f in frames] == [("done", {})]


async def test_cancelled_terminal_frame_finalizes_row_as_cancelled(project):
    """A cancelled terminal frame must always finalize durably as cancelled,
    even when no cancel intent was recorded in the database (the shutdown
    path only sets the cancel events): the durable status must match what
    subscribers saw."""
    task_id = await make_queued(project)
    source = scripted_source([
        make_event("delta", {"content": "x"}),
        make_event("cancelled", CANCELLED_PAYLOAD),
    ])

    frames, _ = await run_task(project, task_id, source)

    assert (await get_row(project, task_id))["status"] == "cancelled"
    assert [parse_frame(f)[1:] for f in frames] == [
        ("delta", {"content": "x"}),
        ("cancelled", CANCELLED_PAYLOAD),
    ]


# ---------------------------------------------------------------------------
# Module surface: launch / cancel / shutdown_all / startup_reconcile
# ---------------------------------------------------------------------------


async def test_launch_runs_task_and_detaches_session_when_done(project):
    task_id = await make_queued(project)
    source = scripted_source([make_event("delta", {"content": "hi"})])

    task_runtime.launch(
        task_id=task_id, project_id=project, source_factory=source,
    )

    assert stream_hub.get(task_id) is not None
    await wait_until(lambda: stream_hub.get(task_id) is None)
    assert (await get_row(project, task_id))["status"] == "completed"
    assert task_runtime.cancel(task_id) is False


async def test_cancel_signals_running_source_for_cooperative_winddown(project):
    task_id = await make_queued(project)

    def factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await cancel_event.wait()
        return gen()

    task_runtime.launch(
        task_id=task_id, project_id=project, source_factory=factory,
    )
    await wait_until_status(project, task_id, "running")

    assert task_runtime.cancel(task_id) is True

    await wait_until(lambda: stream_hub.get(task_id) is None)
    # The wind-down ran on the cancel signal, so the row must end cancelled
    # even though the source emitted no terminal frame: a cancelled stream is
    # never recorded as a completion.
    assert (await get_row(project, task_id))["status"] == "cancelled"


async def test_shutdown_all_finalizes_cooperative_and_hard_cancelled_as_cancelled(
    project, monkeypatch,
):
    """Shutdown sets every cancel event and records no cancel intent in the
    database, yet the durable row must match what subscribers saw: a source
    that winds down on the signal ends cancelled with exactly one terminal
    frame, and a hard-cancelled straggler is finalized by its shielded
    CancelledError handler."""
    monkeypatch.setattr(task_runtime, "SHUTDOWN_GRACE_SECONDS", 0.05)

    def cooperative_factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await cancel_event.wait()
            yield make_event("cancelled", CANCELLED_PAYLOAD)
        return gen()

    def stubborn_factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await asyncio.sleep(3600)
        return gen()

    coop_id = await make_queued(project)
    stubborn_id = await make_queued(project)
    task_runtime.launch(
        task_id=coop_id, project_id=project, source_factory=cooperative_factory,
    )
    task_runtime.launch(
        task_id=stubborn_id, project_id=project, source_factory=stubborn_factory,
    )
    await wait_until_status(project, coop_id, "running")
    await wait_until_status(project, stubborn_id, "running")
    frames = []

    async def collect():
        async for frame in stream_hub.get(coop_id).subscribe():
            frames.append(frame)

    collector = asyncio.create_task(collect())

    await asyncio.wait_for(task_runtime.shutdown_all(), timeout=5)
    await asyncio.wait_for(collector, timeout=5)

    assert (await get_row(project, coop_id))["status"] == "cancelled"
    assert (await get_row(project, stubborn_id))["status"] == "cancelled"
    assert stream_hub.get(coop_id) is None
    assert stream_hub.get(stubborn_id) is None

    terminal = [
        parse_frame(f) for f in frames
        if parse_frame(f)[1] in ("done", "error", "cancelled")
    ]
    assert len(terminal) == 1
    assert terminal[0][1:] == ("cancelled", CANCELLED_PAYLOAD)

    # Both rows were finalized durably; nothing is left to reconcile.
    assert await task_runtime.startup_reconcile() == 0


async def test_shutdown_all_drains_a_task_launched_during_the_drain(
    project, monkeypatch,
):
    """A task launched while shutdown was already draining missed the
    cooperative signal; the final registry pass cancels it too, so nothing
    launched in that window keeps running past shutdown_all."""
    # A generous grace keeps the launch-inside-the-drain window deterministic
    # under suite load: the 0.05s sleep must land well before the grace
    # expires, or the late task would miss the cooperative drain entirely.
    monkeypatch.setattr(task_runtime, "SHUTDOWN_GRACE_SECONDS", 1.0)

    def stubborn_factory(cancel_event):
        async def gen():
            yield make_event("delta", {"content": "x"})
            await asyncio.sleep(3600)
        return gen()

    early_id = await make_queued(project)
    task_runtime.launch(
        task_id=early_id, project_id=project, source_factory=stubborn_factory,
    )
    await wait_until_status(project, early_id, "running")

    shutdown = asyncio.create_task(task_runtime.shutdown_all())
    await asyncio.sleep(0.05)  # the snapshot is taken; the drain is waiting
    late_id = await make_queued(project)
    task_runtime.launch(
        task_id=late_id, project_id=project, source_factory=stubborn_factory,
    )
    await wait_until_status(project, late_id, "running")

    await asyncio.wait_for(shutdown, timeout=5)

    assert (await get_row(project, early_id))["status"] == "cancelled"
    assert (await get_row(project, late_id))["status"] == "cancelled"
    assert stream_hub.get(early_id) is None
    assert stream_hub.get(late_id) is None


async def test_startup_reconcile_finalizes_runnable_rows_cancel_aware(project):
    """Rows left runnable by a dead run are failed with the restart reason,
    except cancelling rows which finalize as cancelled (cancel intent is
    preserved); parked checkpoints and terminal rows survive; the sweep is
    idempotent."""
    queued = await make_queued(project)
    running = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_running(running)
    cancelling = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(cancelling)
    parked = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_awaiting_input(parked, {"tool_name": "bash"})
    completed = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(completed)

    finalized = await task_runtime.startup_reconcile()

    assert finalized == 3
    assert (await get_row(project, queued))["status"] == "failed"
    assert (await get_row(project, running))["status"] == "failed"
    for task_id in (queued, running):
        assert (await get_row(project, task_id))["error"] == RESTART_ERROR
    assert (await get_row(project, cancelling))["status"] == "cancelled"
    assert (await get_row(project, cancelling))["error"] is None
    assert (await get_row(project, parked))["status"] == "awaiting_input"
    assert (await get_row(project, completed))["status"] == "completed"

    assert await task_runtime.startup_reconcile() == 0


async def test_startup_reconcile_recovers_consuming_interaction(project):
    task_id = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_awaiting_input(task_id, {
            "checkpoint": {
                "task_id": task_id,
                "interaction_id": "interaction-1",
                "interaction_type": "permission",
            },
            "interaction_data": {"interaction_type": "permission"},
        })
        assert await uow.task_state.claim_interaction(
            task_id, "interaction-1", "permission",
        )

    assert await task_runtime.startup_reconcile() == 1
    row = await get_row(project, task_id)
    assert row["status"] == "interaction_failed"
    assert row["interaction_state"]["checkpoint"]["interaction_id"] == "interaction-1"
    assert "retry" in row["error"]
    assert await task_runtime.startup_reconcile() == 0


async def test_runner_finalizes_when_project_is_deleting(project, monkeypatch):
    """Delete/reset barriers still allow a runner to record its final state."""
    task_id = await make_queued(project)
    monkeypatch.setattr(task_runtime, "is_project_active", lambda _: False)
    session = stream_hub.create(task_id, project)
    runner = TaskRunner(
        task_id=task_id,
        project_id=project,
        source_factory=lambda cancel_event: scripted_source([])(cancel_event),
        session=session,
    )

    await runner.run()

    row = await get_row(project, task_id)
    assert row["status"] == "failed"
    assert row["error"] == "Project is inactive."


# ---------------------------------------------------------------------------
# Stranded-row sweep (live-process recovery)
# ---------------------------------------------------------------------------


async def _backdate_row(project, task_id, seconds=1000.0):
    """Rewind a row's ``updated_at`` past the sweep's grace window."""
    stale = (utcnow() - timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')
    async with UnitOfWork(project) as uow:
        await uow.session.execute(
            update(TaskState)
            .where(TaskState.task_id == task_id)
            .values(updated_at=stale)
        )
        await uow.commit()


async def test_sweep_finalizes_stranded_rows_cancel_aware(project):
    """Runnable rows with no live runner and an expired grace window are
    finalized: queued/running as failed with the sweep reason, cancelling as
    cancelled with no error."""
    stranded_queued = await make_queued(project)
    stranded_running = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_running(stranded_running)
    stranded_cancelling = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(stranded_cancelling)
    for task_id in (stranded_queued, stranded_running, stranded_cancelling):
        await _backdate_row(project, task_id)

    finalized = await task_runtime._sweep_stranded_rows()

    assert finalized == 3
    for task_id in (stranded_queued, stranded_running):
        row = await get_row(project, task_id)
        assert row["status"] == "failed"
        assert row["error"] == task_runtime.STRANDED_ERROR_MESSAGE
    cancelling = await get_row(project, stranded_cancelling)
    assert cancelling["status"] == "cancelled"
    assert cancelling["error"] is None

    # The sweep is idempotent and startup reconciliation finds nothing left.
    assert await task_runtime._sweep_stranded_rows() == 0
    assert await task_runtime.startup_reconcile() == 0


async def test_sweep_leaves_fresh_rows_within_the_grace_window(project):
    """A row just claimed by a launch has a fresh updated_at: the grace
    window keeps the sweep from finalizing it before its runner appears."""
    fresh_queued = await make_queued(project)
    fresh_running = await make_queued(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_running(fresh_running)

    assert await task_runtime._sweep_stranded_rows() == 0
    assert (await get_row(project, fresh_queued))["status"] == "queued"
    assert (await get_row(project, fresh_running))["status"] == "running"


async def test_sweep_skips_rows_with_a_live_runner_or_session(project):
    """A runnable row is only stranded when no in-process runner registry
    entry (or hub session) exists for its task id."""
    runner_id = await make_queued(project)
    session_id = await make_queued(project)
    await _backdate_row(project, runner_id)
    await _backdate_row(project, session_id)

    holder = asyncio.create_task(asyncio.sleep(3600))
    task_runtime._tasks[runner_id] = holder
    held_session = stream_hub.create(session_id, project)
    try:
        assert await task_runtime._sweep_stranded_rows() == 0
        assert (await get_row(project, runner_id))["status"] == "queued"
        assert (await get_row(project, session_id))["status"] == "queued"
    finally:
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        task_runtime._tasks.pop(runner_id, None)
        stream_hub.remove(session_id, held_session)


async def test_stranded_sweep_loop_finalizes_rows_until_stopped(
    project, monkeypatch,
):
    """The lifecycle-owned loop sweeps periodically until stopped, so a row
    stranded while the process lives is recovered without a restart."""
    monkeypatch.setattr(task_runtime, "STRANDED_SWEEP_INTERVAL_SECONDS", 0.01)
    stranded = await make_queued(project)
    await _backdate_row(project, stranded)

    task_runtime.start_stranded_sweep()
    try:
        await wait_until_status(project, stranded, "failed")
    finally:
        await task_runtime.stop_stranded_sweep()

    row = await get_row(project, stranded)
    assert row["error"] == task_runtime.STRANDED_ERROR_MESSAGE
