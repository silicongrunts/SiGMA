import pytest
from sqlalchemy import update

from app.database.models import TaskState
from app.database.repos.task_state_repo import TaskStateRepository


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
