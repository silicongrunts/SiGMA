"""submit_chat session-claim contract.

The serialized claim (one runnable task_state row per session, backed by a
partial unique index) gates every submission path: new messages, resumes,
edit-and-submit, and the _claim_chat_task retry/conflict handling. All
runner launches are stubbed; the database is real.
"""

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.exceptions import TaskActiveError, ValidationError
from app.core.task_status import (
    STATUS_CANCELLING,
    STATUS_QUEUED,
    STATUS_RUNNING,
)
from app.core.utils import generate_id
from app.database.repos.task_state_repo import TaskStateRepository
from app.database.unit_of_work import UnitOfWork
from app.services import task_runtime
from app.services.ai_service import ai_service
from app.core.config import settings
from tests.ai.matrix_harness import (  # noqa: F401 (autouse fixture below)
    load_state,
    load_state_row,
    make_turn,
    owned_response,
    park_permission_checkpoint,
    use_fixture_project_root,
)


@pytest.mark.asyncio
async def test_submit_chat_rejects_new_message_while_parked(project, monkeypatch):
    """A parked (awaiting_input) session refuses new messages: the pending
    prompt must be answered or the task cancelled before a fresh turn may
    claim the session — same standard as edit_and_submit_chat."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, task_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, task_id)

    with pytest.raises(TaskActiveError):
        await ai_service.submit_chat(project, "new message", {}, session_id=session_id)

    assert launched == []


@pytest.mark.asyncio
async def test_submit_chat_rejects_when_runnable_task_holds_session(project, monkeypatch):
    """A queued/running row holds the session's claim: a second submission is
    refused before any message write or runner launch."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, _ = await make_turn(project)  # leaves a queued row

    with pytest.raises(TaskActiveError):
        await ai_service.submit_chat(project, "second message", {}, session_id=session_id)

    assert launched == []


@pytest.mark.asyncio
async def test_submit_chat_resume_claims_session_and_keeps_checkpoint(project, monkeypatch):
    """A resume is the one submission allowed against a parked session: it
    inserts the new queued row (the claim) while the parked checkpoint stays
    put for the runner to consume, and launches exactly one streaming task
    carrying the interaction response."""
    launched = []
    sources = []

    def fake_launch(**kwargs):
        launched.append(kwargs)
        # The runner calls the source factory with its cancel event.
        kwargs["source_factory"](asyncio.Event())

    monkeypatch.setattr(task_runtime, "launch", fake_launch)
    monkeypatch.setattr(
        "app.services.chat_executor.stream_chat_for_task",
        lambda **kwargs: sources.append(kwargs),
    )
    session_id, parked_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, parked_id)

    response = await owned_response(project, session_id, {"approved": False, "reason": "not yet"})
    result = await ai_service.submit_chat(
        project, "", {}, session_id=session_id, resume=True,
        interaction_response=response,
    )

    assert result["task_id"] != parked_id
    status, interaction, _ = await load_state(project, session_id, parked_id)
    assert status == "awaiting_input"
    assert interaction["tool_name"] == "bash"

    async with UnitOfWork(project) as uow:
        resumed = await uow.task_state.get_by_id(result["task_id"])
    assert resumed["status"] == "queued"

    # Exactly one streaming task launched, into the project's session, and
    # its source carries the resume's interaction response.
    assert len(launched) == 1
    assert launched[0]["task_id"] == result["task_id"]
    assert launched[0]["project_id"] == project
    assert len(sources) == 1
    assert sources[0]["interaction_response"] == response
    assert sources[0]["session_id"] == session_id


@pytest.mark.asyncio
async def test_submit_chat_rejects_resume_carrying_a_message(project, monkeypatch):
    """A resume answers the parked checkpoint only: any non-empty message
    would insert a user row between the parked tool call and its result —
    an invalid provider sequence — so it is refused before any write."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, parked_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, parked_id)

    with pytest.raises(ValidationError):
        await ai_service.submit_chat(
            project, "a new message", {}, session_id=session_id, resume=True,
        )

    assert launched == []
    _, _, messages = await load_state(project, session_id, parked_id)
    assert not [m for m in messages if m.role == "user" and "a new message" in m.content]


@pytest.mark.asyncio
async def test_submit_chat_rejects_resume_with_compact_command(project, monkeypatch):
    """A /compact resume would bury the parked checkpoint under a compaction
    boundary — the combination is refused."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, parked_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, parked_id)

    with pytest.raises(ValidationError):
        await ai_service.submit_chat(
            project, "/compact", {}, session_id=session_id, resume=True,
        )

    assert launched == []


@pytest.mark.asyncio
async def test_submit_chat_rejects_resume_without_interaction_response(
    project, monkeypatch,
):
    """A resume without a response would run a fresh turn over the parked
    session and leave the checkpoint blocking every later message — refused
    before any claim or launch. Every frontend resume caller sends the
    response it rendered, so only a malformed request can hit this."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, parked_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, parked_id)

    for response in (None, {}):
        with pytest.raises(ValidationError):
            await ai_service.submit_chat(
                project, "", {}, session_id=session_id, resume=True,
                interaction_response=response,
            )

    assert launched == []
    status, interaction, _ = await load_state(project, session_id, parked_id)
    assert status == "awaiting_input"
    assert interaction is not None


@pytest.mark.asyncio
async def test_submit_chat_rejects_interaction_response_without_resume(
    project, monkeypatch,
):
    """A non-resume submit carrying an interaction response has no checkpoint
    to feed: the loop would take the resume path, find no parked tool call,
    and end with a bare done — a persisted message and a launched task with
    no LLM turn. Refused before the claim or any message write."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()

    with pytest.raises(ValidationError):
        await ai_service.submit_chat(
            project, "hello", {}, session_id=session.id,
            interaction_response={"approved": True},
        )

    assert launched == []
    _, _, messages = await load_state(project, session.id, None)
    assert messages == []


@pytest.mark.asyncio
async def test_submit_chat_rejects_empty_submit(project, monkeypatch):
    """A submit with no text and no attachments would launch a real LLM turn
    with nothing to say — refused before any session creation or claim."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))

    with pytest.raises(ValidationError):
        await ai_service.submit_chat(project, "", {})

    assert launched == []


@pytest.mark.asyncio
async def test_submit_chat_resume_rejects_nonexistent_session(project, monkeypatch):
    """A resume against a session row that does not exist is a stale client
    (the frontend restore flow reads the session from history first): it is
    refused instead of silently minting an empty session shell. A resume
    against an EXISTING session stays legitimate."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))

    with pytest.raises(ValidationError):
        await ai_service.submit_chat(
            project, "", {}, session_id="no-such-session", resume=True,
            interaction_response={"approved": True},
        )

    assert launched == []
    async with UnitOfWork(project) as uow:
        assert await uow.sessions.get_by_id("no-such-session") is None
        assert await uow.sessions.list_all() == []


@pytest.mark.asyncio
async def test_submit_chat_accepts_attachment_only_message(project, monkeypatch):
    """An image-only submit (no text, attachments present) is a legitimate
    message: the persisted user row carries the rendered attachments content
    the LLM turn will see, and the task claims and launches normally."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()

    # A real file backs the attachment, as the upload route would leave it.
    attachments_dir = (
        settings.get_project_path(project) / ".SiGMA" / "chat_attachments"
    )
    attachments_dir.mkdir(parents=True)
    (attachments_dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    result = await ai_service.submit_chat(
        project, "",
        {"attachments": [{
            "path": ".SiGMA/chat_attachments/a.png",
            "mime_type": "image/png",
        }]},
        session_id=session.id,
    )

    assert len(launched) == 1
    assert launched[0]["task_id"] == result["task_id"]
    row = await load_state_row(project, result["task_id"])
    assert row["status"] == "queued"

    _, _, messages = await load_state(project, session.id, result["task_id"])
    user_rows = [m for m in messages if m.role == "user"]
    assert len(user_rows) == 1
    assert "<attachments>" in user_rows[0].content
    assert ".SiGMA/chat_attachments/a.png" in user_rows[0].content


@pytest.mark.asyncio
async def test_submit_chat_finalizes_row_when_launch_fails(project, monkeypatch):
    """A launch failure after the claim committed fails the row instead of
    leaving it queued — a stranded queued row would block the session."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
    seen = {}

    def failing_launch(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(task_runtime, "launch", failing_launch)

    with pytest.raises(RuntimeError):
        await ai_service.submit_chat(project, "hello", {}, session_id=session.id)

    row = await load_state_row(project, seen["task_id"])
    assert row["status"] == "failed"
    assert "runner exploded" in row["error"]


@pytest.mark.asyncio
async def test_submit_chat_finalizes_row_when_cancelled_after_claim(
    project, monkeypatch,
):
    """A cancellation landing between the claim commit and the runner launch
    is a BaseException the old ``except Exception`` would have let through:
    the row must still be finalized before the cancellation propagates."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
    seen = {}

    def cancelling_launch(**kwargs):
        seen.update(kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(task_runtime, "launch", cancelling_launch)

    with pytest.raises(asyncio.CancelledError):
        await ai_service.submit_chat(project, "hello", {}, session_id=session.id)

    row = await load_state_row(project, seen["task_id"])
    assert row["status"] == "failed"
    assert row["error"] == "Task was cancelled before it started."


@pytest.mark.asyncio
async def test_claim_chat_task_conflict_raises_task_active_without_takeover(project):
    """When the claim's serialized guard finds another live submission's
    row, the conflict surfaces as TaskActiveError naming the existing row —
    no retry, no takeover, and the winning row is intact."""
    session_id, holder_id = await make_turn(project)  # queued row holds the claim

    with pytest.raises(TaskActiveError) as excinfo:
        await ai_service._claim_chat_task(
            project, session_id, generate_id(),
            rejected={STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING},
        )

    assert excinfo.value.details["task_id"] == holder_id
    holder = await load_state_row(project, holder_id)
    assert holder["status"] == "queued"


@pytest.mark.asyncio
async def test_claim_insert_conflict_with_live_row_raises_task_active(
    project, monkeypatch,
):
    """A claim insert rejected by the unique index while a live conflicting
    row exists surfaces that row's id — never an empty task_id."""
    session_id, holder_id = await make_turn(project)  # queued row holds the claim

    async def conflicting_set_queued(self, *args, **kwargs):
        raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(TaskStateRepository, "set_queued", conflicting_set_queued)

    with pytest.raises(TaskActiveError) as excinfo:
        await ai_service._claim_chat_task(
            project, session_id, generate_id(),
            rejected={STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING},
        )

    assert excinfo.value.details["task_id"] == holder_id
    holder = await load_state_row(project, holder_id)
    assert holder["status"] == "queued"


@pytest.mark.asyncio
async def test_claim_retries_once_after_stale_conflict(project, monkeypatch):
    """A unique-index conflict whose conflicting row already finalized is
    stale: the claim retries the insert once and lands instead of failing
    the submit with an empty task_id."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
    session_id = session.id
    calls = {"n": 0}
    real_set_queued = TaskStateRepository.set_queued

    async def flaky_set_queued(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed"))
        await real_set_queued(self, *args, **kwargs)

    monkeypatch.setattr(TaskStateRepository, "set_queued", flaky_set_queued)

    task_id = generate_id()
    await ai_service._claim_chat_task(
        project, session_id, task_id,
        rejected={STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING},
    )

    assert calls["n"] == 2
    row = await load_state_row(project, task_id)
    assert row["status"] == "queued"


@pytest.mark.asyncio
async def test_claim_conflict_after_retry_raises_task_active_not_integrity_error(
    project, monkeypatch,
):
    """When the retry attempt also conflicts with a row that finalized in the
    gap, the claim still ends in the caller-visible contract — TaskActiveError
    (409) — never a raw IntegrityError (500)."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()

    async def conflicting_set_queued(self, *args, **kwargs):
        raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed"))

    async def no_active_row(self, session_id):
        return None  # the conflicting writer finalized before every re-read

    monkeypatch.setattr(TaskStateRepository, "set_queued", conflicting_set_queued)
    monkeypatch.setattr(TaskStateRepository, "get_active_by_session", no_active_row)

    with pytest.raises(TaskActiveError):
        await ai_service._claim_chat_task(
            project, session.id, generate_id(),
            rejected={STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING},
        )


@pytest.mark.asyncio
async def test_claim_prune_failure_does_not_fail_committed_claim(
    project, monkeypatch,
):
    """Terminal-row pruning is housekeeping after the claim committed: a
    transient failure in the prune must not fail the submission — the claim
    row stays queued and owns the session."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()

    async def failing_prune(self, session_id, keep=5):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(TaskStateRepository, "prune_terminal_by_session", failing_prune)

    task_id = generate_id()
    await ai_service._claim_chat_task(
        project, session.id, task_id,
        rejected={STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING},
    )

    row = await load_state_row(project, task_id)
    assert row["status"] == "queued"


@pytest.mark.asyncio
async def test_claim_retry_treats_own_committed_row_as_success(project, monkeypatch):
    """A transient failure after the claim's insert committed must not turn
    the retry into a self-collision: the retry's guard read sees this
    submit's own queued row and the claim reports success instead of a 409
    with no runner attached."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
    task_id = generate_id()
    real_set_queued = TaskStateRepository.set_queued
    calls = {"n": 0}

    async def commit_then_fail(self, *args, **kwargs):
        await real_set_queued(self, *args, **kwargs)
        calls["n"] += 1
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(TaskStateRepository, "set_queued", commit_then_fail)

    await ai_service._claim_chat_task(
        project, session.id, task_id,
        rejected={STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING},
    )

    assert calls["n"] == 1  # the retry returned at the self-collision guard
    row = await load_state_row(project, task_id)
    assert row["status"] == "queued"
