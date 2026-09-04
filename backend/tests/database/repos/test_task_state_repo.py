import pytest
from uuid import uuid4
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.database.models import TaskState
from app.database.repos.task_state_repo import TaskStateRepository


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
async def test_claim_interaction_is_exact_and_idempotent(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {
            "checkpoint": {"interaction_id": "interaction-1", "interaction_type": "permission"},
            "interaction_data": {"interaction_type": "permission"},
        })

        assert await repo.claim_interaction("task-1", "old", "permission") is None
        claimed = await repo.claim_interaction("task-1", "interaction-1", "permission")
        assert claimed["checkpoint"]["interaction_id"] == "interaction-1"
        assert await repo.claim_interaction("task-1", "interaction-1", "permission") is None


@pytest.mark.asyncio
async def test_failed_interaction_keeps_checkpoint_for_retry(db_session_factory):
    """A crash after claim is recoverable without guessing by session."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {
            "checkpoint": {
                "task_id": "task-1",
                "interaction_id": "interaction-1",
                "interaction_type": "permission",
            },
            "interaction_data": {"interaction_type": "permission"},
        })

        assert await repo.claim_interaction("task-1", "interaction-1", "permission")
        assert await repo.fail_interaction("task-1", "provider unavailable")

        state = await repo.get_pending_interaction("task-1", "interaction-1", "permission")
        assert state["checkpoint"]["task_id"] == "task-1"
        assert (await repo.get_by_id("task-1"))["status"] == "interaction_failed"
        assert await repo.claim_interaction("task-1", "old", "permission") is None


@pytest.mark.asyncio
async def test_owner_checkpoint_index_spans_all_interaction_statuses(
    db_session_factory,
):
    """The parked-checkpoint partial index spans every interaction lifecycle
    status: two rows for the same owner may not hold checkpoints in
    *different* interaction states either.

    Same-status rejection for all three statuses is pinned against the real
    schema in ``test_migration_integrity.py``; the runnable/parked interplay
    is covered by the repo-level tests below. This test covers the remaining
    surface: cross-status collisions within the parked set.
    """
    async with db_session_factory() as db:
        owner = f"session-{uuid4().hex}"
        db.add(TaskState(
            task_id=f"task-{uuid4().hex}", owner_type="chat_session",
            owner_id=owner, status="interaction_consuming",
            interaction_state='{"checkpoint": {}}',
        ))
        await db.flush()
        db.add(TaskState(
            task_id=f"task-{uuid4().hex}", owner_type="chat_session",
            owner_id=owner, status="interaction_failed",
            interaction_state='{"checkpoint": {}}',
        ))
        with pytest.raises(IntegrityError):
            await db.flush()


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


# ---------------------------------------------------------------------------
# Corrupt-checkpoint self-heal (corrupt = stale garbage, delete to release)
# ---------------------------------------------------------------------------


async def _corrupt_checkpoint(db, task_id):
    """Rewrite a parked row's interaction_state as truncated JSON, as a
    half-written or hand-edited row would look."""
    await db.execute(
        update(TaskState)
        .where(TaskState.task_id == task_id)
        .values(interaction_state='{"tool_name": ')
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.database
async def test_corrupt_checkpoint_self_heals_on_pending_read(db_session_factory):
    """A parked row whose checkpoint JSON is corrupt can never resume but
    keeps holding the session claim: the pending read deletes the stale row
    and returns None, so the caller's "nothing pending" cleanup releases the
    session instead of sticking it forever with no dialog to answer."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {"tool_name": "bash"})
        await _corrupt_checkpoint(db, "task-1")

        assert await repo.get_pending_interaction_by_session("session-1") is None
        assert await repo.get_by_id("task-1") is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_corrupt_checkpoint_releases_active_claim(db_session_factory):
    """getActive stops reporting a corrupt parked row: after the heal the
    session reads as idle, so the submit gate no longer refuses with 409."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {"tool_name": "bash"})
        await _corrupt_checkpoint(db, "task-1")

        assert await repo.get_active_by_session("session-1") is None
        assert await repo.get_by_id("task-1") is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_corrupt_checkpoint_heal_keeps_coexisting_runnable_claim(
    db_session_factory,
):
    """Deleting the stale parked row must not report the owner free while a
    resume's queued row still holds the runnable claim: the read re-runs
    after the heal."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("parked", session_id="session-1")
        await repo.mark_awaiting_input("parked", {"tool_name": "bash"})
        await repo.set_queued("resume", session_id="session-1")
        await _corrupt_checkpoint(db, "parked")

        active = await repo.get_active_by_session("session-1")

        assert active is not None
        assert active["task_id"] == "resume"
        assert await repo.get_by_id("parked") is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_parseable_checkpoint_is_never_healed_away(db_session_factory):
    """Self-heal only touches corrupt JSON: a parked row with a parseable
    checkpoint keeps its row, its pending state, and its active claim."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {"tool_name": "bash"})

        state = await repo.get_pending_interaction_by_session("session-1")
        active = await repo.get_active_by_session("session-1")

        assert state == {"tool_name": "bash"}
        assert active is not None
        assert active["status"] == "awaiting_input"
        assert active["interaction_state"] == {"tool_name": "bash"}
        assert await repo.get_by_id("task-1") is not None


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


# ---------------------------------------------------------------------------
# Queued-to-running transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_mark_running_promotes_queued_row(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")

        assert await repo.mark_running("task-1") is True

        task = await repo.get_by_id("task-1")
        assert task["status"] == "running"
        assert task["updated_at"] is not None


@pytest.mark.asyncio
@pytest.mark.database
async def test_mark_running_ignores_non_queued_rows(db_session_factory):
    """The guarded promotion only applies to a queued row: a cancel recorded
    before the runner starts stays authoritative."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.request_cancel("task-1")

        assert await repo.mark_running("task-1") is False

        assert (await repo.get_by_id("task-1"))["status"] == "cancelling"

        await repo.mark_cancelled("task-1")
        assert await repo.mark_running("task-1") is False
        assert (await repo.get_by_id("task-1"))["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Startup reconciliation sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_fail_all_runnable_finalizes_runnable_rows_cancel_aware(
    db_session_factory,
):
    """Cancelling rows finalize as cancelled with no error — the same
    cancel-aware mapping mark_failed applies — so a recorded cancel intent
    is never erased into a failure; only queued/running rows get the
    interrupted-failed treatment."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("queued-task", session_id="session-1")
        await repo.set_queued("running-task", session_id="session-2")
        await repo.mark_running("running-task")
        await repo.set_queued("cancelling-task", session_id="session-3")
        await repo.request_cancel("cancelling-task")

        counts = await repo.fail_all_runnable("Task was interrupted.")

        assert counts == {"failed": 2, "cancelled": 1}
        for task_id in ("queued-task", "running-task"):
            task = await repo.get_by_id(task_id)
            assert task["status"] == "failed"
            assert task["error"] == "Task was interrupted."
        cancelling = await repo.get_by_id("cancelling-task")
        assert cancelling["status"] == "cancelled"
        assert cancelling["error"] is None


@pytest.mark.asyncio
@pytest.mark.database
async def test_fail_all_runnable_leaves_awaiting_input_and_terminal_rows(db_session_factory):
    """A parked interaction checkpoint must survive a restart so the user can
    resume it; already-terminal rows are likewise untouched."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("parked-task", session_id="session-1")
        await repo.mark_awaiting_input("parked-task", {"tool_name": "x"})
        await repo.set_queued("completed-task", session_id="session-2")
        await repo.mark_completed("completed-task")
        await repo.set_queued("failed-task", session_id="session-3")
        await repo.mark_failed("failed-task", "boom")
        await repo.set_queued("cancelled-task", session_id="session-4")
        await repo.request_cancel("cancelled-task")
        await repo.mark_cancelled("cancelled-task")

        counts = await repo.fail_all_runnable("Task was interrupted.")

        assert counts == {"failed": 0, "cancelled": 0}
        assert (await repo.get_by_id("parked-task"))["status"] == "awaiting_input"
        assert (await repo.get_by_id("completed-task"))["status"] == "completed"
        assert (await repo.get_by_id("failed-task"))["error"] == "boom"
        assert (await repo.get_by_id("cancelled-task"))["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.database
async def test_fail_all_runnable_is_idempotent(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")

        assert await repo.fail_all_runnable("Task was interrupted.") == {
            "failed": 1, "cancelled": 0,
        }
        assert await repo.fail_all_runnable("Task was interrupted.") == {
            "failed": 0, "cancelled": 0,
        }


@pytest.mark.asyncio
@pytest.mark.database
async def test_get_stale_runnable_filters_by_cutoff_and_status(db_session_factory):
    """Only runnable rows older than the cutoff are returned: a parked
    interaction checkpoint is never stale-recoverable, and fresh rows (a
    claim still within its grace window) are excluded."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("stale-queued", session_id="session-1")
        await repo.set_queued("stale-running", session_id="session-2")
        await repo.mark_running("stale-running")
        await repo.set_queued("fresh-claimed", session_id="session-3")
        await repo.set_queued("parked", session_id="session-4")
        await repo.mark_awaiting_input("parked", {"tool_name": "x"})

        stale = await repo.get_stale_runnable("2100-01-01 00:00:00")
        assert sorted(row["task_id"] for row in stale) == [
            "fresh-claimed", "stale-queued", "stale-running",
        ]

        assert await repo.get_stale_runnable("2000-01-01 00:00:00") == []


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
        await repo.mark_running("task-1")

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
async def test_mark_completed_finalizes_cancelling_row_as_completed(
    db_session_factory,
):
    """The runner only calls mark_completed after its checks fixed the
    outcome as completed, so a cancelling row is finalized completed — the
    write records exactly the terminal state the subscriber saw."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_running("task-1")
        await repo.request_cancel("task-1")

        await repo.mark_completed("task-1")

        assert (await repo.get_by_id("task-1"))["status"] == "completed"


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
        await repo.mark_running("task-1")
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
async def test_mark_cancelled_finalizes_runnable_rows(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_running("task-1")

        # The runner observed a cancelled terminal frame: the row must follow,
        # whether or not cancel intent was durably recorded first.
        await repo.mark_cancelled("task-1")
        assert (await repo.get_by_id("task-1"))["status"] == "cancelled"

        # Terminal and input-paused rows are never touched.
        await repo.set_queued("task-2", session_id="session-2")
        await repo.mark_completed("task-2")
        await repo.mark_cancelled("task-2")
        assert (await repo.get_by_id("task-2"))["status"] == "completed"

        await repo.set_queued("task-3", session_id="session-3")
        await repo.mark_awaiting_input("task-3", {"tool_name": "bash"})
        await repo.mark_cancelled("task-3")
        assert (await repo.get_by_id("task-3"))["status"] == "awaiting_input"

        await repo.set_queued("task-4", session_id="session-4")
        await repo.request_cancel("task-4")
        await repo.mark_cancelled("task-4")
        assert (await repo.get_by_id("task-4"))["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.database
async def test_mark_awaiting_input_parks_from_running(db_session_factory):
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_running("task-1")

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
        await repo.mark_running("task-1")
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
        await repo.mark_running("task-1")
        await repo.request_cancel("task-1")

        active = await repo.get_active_by_session("session-1")
        assert active is not None
        assert active["status"] == "cancelling"


# ---------------------------------------------------------------------------
# One-active-per-owner partial unique indexes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.concurrency
async def test_second_runnable_task_for_same_owner_rejected(db_session_factory):
    """The partial unique index, not a read-then-write guard, serializes
    concurrent submissions: a second runnable row for the same owner must
    fail in the database."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")

        with pytest.raises(IntegrityError):
            await repo.set_queued("task-2", session_id="session-1")
        await db.rollback()

        task = await repo.get_by_id("task-1")
        assert task["status"] == "queued"


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.concurrency
async def test_resume_queued_row_coexists_with_parked_checkpoint(db_session_factory):
    """A resume legitimately holds the old parked checkpoint and its new
    queued row at the same time; the runnable index must allow that."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {"tool_name": "ask_user_question"})

        await repo.set_queued("task-2", session_id="session-1")
        await repo.mark_running("task-2")

        assert (await repo.get_by_id("task-1"))["status"] == "awaiting_input"
        assert (await repo.get_by_id("task-2"))["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.database
@pytest.mark.concurrency
async def test_second_parked_checkpoint_for_same_owner_rejected(db_session_factory):
    """At most one interaction checkpoint per owner: parking a second row
    while another stays parked must fail, or the resume target is ambiguous."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("task-1", session_id="session-1")
        await repo.mark_awaiting_input("task-1", {"tool_name": "x"})

        await repo.set_queued("task-2", session_id="session-1")
        await repo.mark_running("task-2")

        with pytest.raises(IntegrityError):
            await repo.mark_awaiting_input("task-2", {"tool_name": "y"})
        await db.rollback()

        assert (await repo.get_by_id("task-1"))["status"] == "awaiting_input"
        assert (await repo.get_by_id("task-2"))["status"] == "running"


# ---------------------------------------------------------------------------
# Terminal-row pruning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.database
async def test_prune_terminal_by_session_keeps_most_recent_rows(db_session_factory):
    """Old terminal rows are deleted, the most recent *keep* survive."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)

        for i in range(8):
            task_id = f"task-{i}"
            await repo.set_queued(task_id, session_id="session-1")
            await repo.mark_completed(task_id)
            # created_at has second precision — space the rows out so the
            # "most recent" ordering is deterministic.
            await db.execute(
                update(TaskState)
                .where(TaskState.task_id == task_id)
                .values(
                    created_at=f"2026-01-01 00:00:{i:02d}",
                    updated_at=f"2026-01-01 00:00:{i:02d}",
                )
            )
            await db.commit()

        pruned = await repo.prune_terminal_by_session("session-1", keep=3)

        assert pruned == 5
        # task-0..4 are the oldest — deleted; the 3 most recent survive.
        for i in range(5):
            assert await repo.get_by_id(f"task-{i}") is None
        for i in range(5, 8):
            assert await repo.get_by_id(f"task-{i}") is not None


@pytest.mark.asyncio
@pytest.mark.database
async def test_prune_terminal_by_session_never_touches_active_rows(db_session_factory):
    """A parked checkpoint must survive pruning — it is not terminal."""
    async with db_session_factory() as db:
        repo = TaskStateRepository(db)
        await repo.set_queued("parked-task", session_id="session-1")
        await repo.mark_awaiting_input("parked-task", {"tool_name": "bash"})
        await repo.set_queued("done-task", session_id="session-1")
        await repo.mark_completed("done-task")

        pruned = await repo.prune_terminal_by_session("session-1", keep=0)

        assert pruned == 1
        task = await repo.get_by_id("parked-task")
        assert task["status"] == "awaiting_input"
        assert task["interaction_state"]["tool_name"] == "bash"
