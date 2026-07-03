import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.core.utils import utcnow
from app.database.models import TaskState
from app.database.repos.task_state_repo import TaskStateRepository


def _old_timestamp(seconds_ago: int) -> str:
    """An ISO timestamp far enough in the past to be considered stale."""
    return (utcnow() - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.mark.asyncio
async def test_heartbeat_does_not_overwrite_awaiting_input(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)

        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {
            "tool_name": "ask_user_question",
            "tool_call_id": "call-1",
            "interaction_data": {"interaction_type": "ask_user_question"},
        })

        await repo.heartbeat("task-1")

        task = await repo.get_by_id("task-1")
        assert task["status"] == "awaiting_input"
        assert task["interaction_state"]["tool_name"] == "ask_user_question"


@pytest.mark.asyncio
async def test_pending_interaction_finds_active_checkpoint(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)

        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {
            "tool_name": "ask_user_question",
            "tool_call_id": "call-1",
            "interaction_data": {"interaction_type": "ask_user_question"},
        })

        state = await repo.get_pending_interaction_by_session("session-1")

        assert state is not None
        assert state["tool_name"] == "ask_user_question"


@pytest.mark.asyncio
async def test_pending_interaction_excludes_null_session_tasks(db_session_factory):
    """Annotation tasks (session_id=NULL by design) must not surface in chat reads.

    Regression guard: the old ``get_interaction_state`` had a Layer 2 fallback
    that scanned all awaiting_input rows regardless of session_id, which let
    annotation pending state leak into chat sessions. This test locks the
    product rule that chat reads are strictly session-scoped.
    """
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)

        # An annotation task — no session_id, awaiting input
        await repo.set_queued(
            "annotation-task",
            task_type="annotation_reply",
            owner_type="annotation",
            owner_id="annotation-1",
        )
        await repo.mark_awaiting_input("annotation-task", {"tool_name": "x"})

        # A chat session asks for its own pending state
        state = await repo.get_pending_interaction_by_session("chat-session")

        assert state is None


@pytest.mark.asyncio
async def test_active_annotation_reply_uses_explicit_owner(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)

        await repo.set_queued(
            "annotation-task",
            task_type="annotation_reply",
            owner_type="annotation",
            owner_id="annotation-1",
        )

        active = await repo.get_active_annotation_reply("annotation-1")

        assert active is not None
        assert active["task_id"] == "annotation-task"
        assert active["owner_type"] == "annotation"
        assert active["owner_id"] == "annotation-1"


@pytest.mark.asyncio
async def test_liveness_treats_malformed_active_timestamps_as_stale(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)

        await repo.set_queued("bad-queued", session_id="session-1")
        await db.execute(
            update(TaskState)
            .where(TaskState.task_id == "bad-queued")
            .values(created_at="not-a-date")
        )
        await db.commit()

        await repo.set_queued("bad-running", session_id="session-1")
        await repo.heartbeat("bad-running")
        await db.execute(
            update(TaskState)
            .where(TaskState.task_id == "bad-running")
            .values(heartbeat_at=None)
        )
        await db.commit()

        assert await repo.check_liveness("bad-queued") == "stale"
        assert await repo.check_liveness("bad-running") == "stale"


# ---------------------------------------------------------------------------
# Durable cancel state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_request_cancel_queued_to_cancelling(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")

        assert await repo.request_cancel("task-1") == "cancelling"

        task = await repo.get_by_id("task-1")
        assert task["status"] == "cancelling"


@pytest.mark.asyncio
@pytest.mark.database
async def test_request_cancel_running_to_cancelling(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")  # queued -> running

        assert await repo.request_cancel("task-1") == "cancelling"


@pytest.mark.asyncio
@pytest.mark.database
async def test_request_cancel_awaiting_input_to_cancelled_clears_state(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {"tool_name": "ask_user_question"})

        assert await repo.request_cancel("task-1") == "cancelled"

        task = await repo.get_by_id("task-1")
        assert task["status"] == "cancelled"
        assert task["interaction_state"] is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_request_cancel_idempotent_on_cancelling(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.request_cancel("task-1")

        # A second cancel must not flip or error.
        assert await repo.request_cancel("task-1") == "cancelling"
        assert (await repo.get_by_id("task-1"))["status"] == "cancelling"


@pytest.mark.asyncio
@pytest.mark.database
async def test_request_cancel_terminal_returns_status(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_completed("task-1")

        assert await repo.request_cancel("task-1") == "completed"
        assert (await repo.get_by_id("task-1"))["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.database
async def test_request_cancel_missing_returns_not_found(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        assert await repo.request_cancel("nope") == "not_found"


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.regression
async def test_heartbeat_preserves_cancelling(db_session_factory):
    """Regression: a heartbeat arriving after cancel must not revert cancelling."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")

        await repo.heartbeat("task-1")  # worker heartbeat during wind-down

        task = await repo.get_by_id("task-1")
        assert task["status"] == "cancelling"
        assert task["heartbeat_at"] is not None


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.regression
async def test_heartbeat_freezes_cancelling_heartbeat(db_session_factory):
    """Regression: a heartbeat must not refresh heartbeat_at for a cancelling
    task. Freezing it is what lets a stuck, non-cooperative worker age into
    staleness so the cancel recovers, instead of hanging on cancelling forever.
    """
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")

        # Pin a sentinel heartbeat, then call heartbeat: a cancelling task must
        # be left untouched so the frozen timestamp can age into staleness.
        await db.execute(
            update(TaskState)
            .where(TaskState.task_id == "task-1")
            .values(heartbeat_at="2000-01-01 00:00:00")
        )
        await db.commit()
        await repo.heartbeat("task-1")

        task = await repo.get_by_id("task-1")
        assert task["status"] == "cancelling"
        assert task["heartbeat_at"] == "2000-01-01 00:00:00"


@pytest.mark.asyncio
@pytest.mark.database
async def test_heartbeat_promotes_queued_to_running(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")

        await repo.heartbeat("task-1")

        assert (await repo.get_by_id("task-1"))["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.regression
async def test_mark_completed_honours_cancelling(db_session_factory):
    """Regression: a worker finishing while cancelled finalizes as cancelled."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")

        await repo.mark_completed("task-1")

        assert (await repo.get_by_id("task-1"))["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.database
async def test_mark_completed_skips_awaiting_input(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {"tool_name": "x"})

        await repo.mark_completed("task-1")

        assert (await repo.get_by_id("task-1"))["status"] == "awaiting_input"


@pytest.mark.asyncio
@pytest.mark.database
async def test_mark_failed_honours_cancelling_and_clears_error(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")

        await repo.mark_failed("task-1", "boom")

        task = await repo.get_by_id("task-1")
        assert task["status"] == "cancelled"
        assert task["error"] is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_mark_failed_does_not_clobber_completed(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_completed("task-1")

        await repo.mark_failed("task-1", "late stale write")

        task = await repo.get_by_id("task-1")
        assert task["status"] == "completed"
        assert task["error"] is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_mark_cancelled_only_from_cancelling(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")

        await repo.mark_cancelled("task-1")  # running, not cancelling -> no-op
        assert (await repo.get_by_id("task-1"))["status"] == "running"

        await repo.request_cancel("task-1")
        await repo.mark_cancelled("task-1")
        assert (await repo.get_by_id("task-1"))["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.database
async def test_check_liveness_cancelling_fresh(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")

        assert await repo.check_liveness("task-1") == "cancelling"


@pytest.mark.asyncio
@pytest.mark.database
async def test_check_liveness_cancelling_stale(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")
        await db.execute(
            update(TaskState)
            .where(TaskState.task_id == "task-1")
            .values(heartbeat_at=_old_timestamp(200))
        )
        await db.commit()

        assert await repo.check_liveness("task-1") == "stale"


@pytest.mark.asyncio
@pytest.mark.database
async def test_check_liveness_cancelling_null_heartbeat_uses_created_age(db_session_factory):
    """A cancel that lands before the worker ever heartbeats must not release the
    lock instantly; it ages from created_at like a queued task."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.request_cancel("task-1")  # no heartbeat yet
        assert await repo.check_liveness("task-1") == "cancelling"

        await db.execute(
            update(TaskState)
            .where(TaskState.task_id == "task-1")
            .values(created_at=_old_timestamp(200))
        )
        await db.commit()
        assert await repo.check_liveness("task-1") == "stale"


@pytest.mark.asyncio
@pytest.mark.database
async def test_check_liveness_cancelled_terminal(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")
        await repo.mark_cancelled("task-1")

        assert await repo.check_liveness("task-1") == "cancelled"


@pytest.mark.asyncio
@pytest.mark.database
async def test_mark_awaiting_input_parks_from_running(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")

        await repo.mark_awaiting_input("task-1", {"tool_name": "ask_user_question"})

        task = await repo.get_by_id("task-1")
        assert task["status"] == "awaiting_input"
        assert task["interaction_state"]["tool_name"] == "ask_user_question"


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.regression
async def test_mark_awaiting_input_does_not_clobber_cancelling(db_session_factory):
    """Regression: an interactive-tool pause arriving after cancel must not
    overwrite cancelling back to awaiting_input."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")  # running -> cancelling

        await repo.mark_awaiting_input("task-1", {"tool_name": "ask_user_question"})

        task = await repo.get_by_id("task-1")
        assert task["status"] == "cancelling"
        assert task["interaction_state"] is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_get_active_by_session_includes_cancelling(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.heartbeat("task-1")
        await repo.request_cancel("task-1")

        active = await repo.get_active_by_session("session-1")
        assert active is not None
        assert active["status"] == "cancelling"


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.concurrency
@pytest.mark.regression
async def test_concurrent_cancel_and_heartbeat_converge_to_cancelling(db_session_factory):
    """Regression for the lost-update window: the guarded UPDATEs must converge
    on cancelling regardless of whether a heartbeat races the cancel."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("race-task", session_id="session-1")
        await repo.heartbeat("race-task")  # queued -> running

    async def _heartbeat():
        async with db_session_factory() as db:
            await TaskStateRepository(db).heartbeat("race-task")

    async def _cancel():
        async with db_session_factory() as db:
            return await TaskStateRepository(db).request_cancel("race-task")

    _, cancel_status = await asyncio.gather(_heartbeat(), _cancel())

    assert cancel_status == "cancelling"
    async with db_session_factory() as db:
        task = await TaskStateRepository(db).get_by_id("race-task")
    assert task["status"] == "cancelling"
    assert task["heartbeat_at"] is not None
