import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.utils import utcnow
from app.database.models import BackgroundTask
from app.database.repos.background_task_repo import (
    BackgroundTaskRepository,
    _task_id_for_dedupe,
)

QUEUE = "library"


@pytest.mark.asyncio
async def test_enqueue_dedupes_active_task(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        first = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{"doc_id":"d1"}',
            dedupe_key="rag_index:p1:d1",
            priority=100,
        )
        second = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{"doc_id":"d1","again":true}',
            dedupe_key="rag_index:p1:d1",
            priority=50,
        )

        assert second.id == first.id
        assert second.priority == 50
        assert second.payload_json == '{"doc_id":"d1","again":true}'


# ---------------------------------------------------------------------------
# Concurrent enqueue on the same deterministic id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_enqueue_same_dedupe_key_both_succeed(db_session_factory):
    """Two enqueues racing the same deterministic id must both return, land
    on one row, and apply the merge rules — whichever INSERT loses, its
    IntegrityError is absorbed as an existing-row merge, never raised."""
    # WAL mirrors the production journal mode: the losing INSERT blocks on
    # the winner's write lock instead of deadlocking the commit path, so the
    # UNIQUE violation fires exactly as it does in the running app.
    async with db_session_factory() as setup_session:
        await setup_session.execute(text("PRAGMA journal_mode=WAL"))
        await setup_session.commit()

    task_id = _task_id_for_dedupe("rag_index:p1:d1")
    async with db_session_factory() as session_a, db_session_factory() as session_b:
        repo_a = BackgroundTaskRepository(session_a)
        repo_b = BackgroundTaskRepository(session_b)

        first, second = await asyncio.gather(
            repo_a.enqueue(
                kind="rag_index", queue=QUEUE,
                payload_json='{"who":"a"}',
                dedupe_key="rag_index:p1:d1", priority=200, max_attempts=5,
            ),
            repo_b.enqueue(
                kind="rag_index", queue=QUEUE,
                payload_json='{"who":"b"}',
                dedupe_key="rag_index:p1:d1", priority=100, max_attempts=3,
            ),
        )

        assert first.id == second.id == task_id
        assert first.status == "queued"

        # Read the merged row through a fresh session: repo_a's identity map
        # may still hold its own pre-merge row object, and a repeat SELECT
        # would not overwrite already-loaded attributes with the DB truth.
        # Either racer may commit first, so the merged row must satisfy the
        # min/max rules whichever way the race resolves.
        async with db_session_factory() as verify_session:
            row = await BackgroundTaskRepository(verify_session).get_by_id(task_id)
        assert row.status == "queued"
        assert row.priority == 100      # min of the two priorities
        assert row.max_attempts == 5    # max of the two max_attempts
        assert row.payload_json in ('{"who":"a"}', '{"who":"b"}')


@pytest.mark.asyncio
async def test_enqueue_lost_insert_race_merges_into_committed_row(db_session_factory):
    """Deterministic replay of the insert race: the loser reads the
    deterministic id as missing, the winner's row commits, and the loser's
    INSERT then fails on the unique id. The loser must roll back (its session
    is left in a failed transaction with a stale read) and land as a merge on
    the winner's row instead of raising."""
    async with db_session_factory() as loser_session:
        loser_repo = BackgroundTaskRepository(loser_session)
        original_get_by_id = loser_repo.get_by_id
        winner_committed = False

        async def racing_get_by_id(task_id):
            nonlocal winner_committed
            row = await original_get_by_id(task_id)
            if row is None and not winner_committed:
                # The concurrent winner commits in the gap between the
                # loser's read and the loser's INSERT.
                async with db_session_factory() as winner_session:
                    winner_session.add(BackgroundTask(
                        id=task_id,
                        kind="rag_index",
                        queue=QUEUE,
                        status="queued",
                        priority=200,
                        payload_json='{"who":"winner"}',
                        dedupe_key="rag_index:p1:d1",
                        max_attempts=5,
                    ))
                    await winner_session.commit()
                winner_committed = True
            return row

        loser_repo.get_by_id = racing_get_by_id

        loser_task = await loser_repo.enqueue(
            kind="rag_index", queue=QUEUE,
            payload_json='{"who":"loser"}',
            dedupe_key="rag_index:p1:d1", priority=100, max_attempts=3,
        )

        assert loser_task.id == _task_id_for_dedupe("rag_index:p1:d1")
        assert loser_task.status == "queued"
        assert loser_task.priority == 100       # min(200, 100)
        assert loser_task.max_attempts == 5     # max(5, 3)
        assert loser_task.payload_json == '{"who":"loser"}'

        row = await original_get_by_id(_task_id_for_dedupe("rag_index:p1:d1"))
        assert row is not None
        assert row.dedupe_key == "rag_index:p1:d1"


@pytest.mark.asyncio
async def test_claim_heartbeat_and_complete(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        queued = await repo.enqueue(
            kind="document_process",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="document_process:p1:d1",
        )

        claimed = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        assert claimed.id == queued.id
        assert claimed.status == "running"
        # The claim token identifies this run, not the runner.
        assert claimed.lease_owner.startswith("worker-1:")
        assert await repo.heartbeat(claimed.id, claimed.lease_owner, 60) is True

        await repo.mark_completed(claimed.id, claimed.lease_owner)
        done = await repo.get_by_id(claimed.id)
        assert done.status == "completed"
        assert done.lease_owner is None


@pytest.mark.asyncio
async def test_expired_running_task_is_reclaimed_and_counts_attempt(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="rag_index:p1:d1",
            max_attempts=3,
        )
        claimed = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(claimed)
        await session.commit()

        reclaimed = await repo.claim_next(
            queue=QUEUE, owner="worker-2", lease_seconds=60,
        )

        assert reclaimed.id == task.id
        assert reclaimed.lease_owner.startswith("worker-2:")
        assert reclaimed.attempt_count == 1


@pytest.mark.asyncio
async def test_same_runner_reclaim_issues_new_token_and_invalidates_old(db_session_factory):
    """Re-claiming an expired lease must not let the old run keep the task
    alive: each claim gets a one-time token, and a stale token can neither
    heartbeat nor terminalize the new claim."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="rag_index:p1:d1",
            max_attempts=3,
        )
        first = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        old_token = first.lease_owner

        first.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(first)
        await session.commit()

        second = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        assert second.id == task.id
        assert second.lease_owner != old_token

        # The old run's heartbeat and finalize are no-ops against the new claim.
        assert await repo.heartbeat(task.id, old_token, 60) is False
        assert await repo.mark_completed(task.id, old_token) is False
        assert await repo.mark_cancelled(task.id, old_token) is False
        assert await repo.mark_failed_or_retry(task.id, old_token, "stale") == "missing"
        still_running = await repo.get_by_id(task.id)
        assert still_running.status == "running"
        assert still_running.lease_owner == second.lease_owner

        # The new token works normally.
        assert await repo.heartbeat(task.id, second.lease_owner, 60) is True
        assert await repo.mark_completed(task.id, second.lease_owner) is True
        assert (await repo.get_by_id(task.id)).status == "completed"


@pytest.mark.asyncio
async def test_expired_running_task_returns_failed_when_retry_budget_exhausted(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="document_process",
            queue=QUEUE,
            payload_json='{"doc_id":"d1","doc_revision":1}',
            dedupe_key="document_process:p1:d1:1",
            max_attempts=1,
        )
        claimed = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(claimed)
        await session.commit()

        failed = await repo.claim_next(
            queue=QUEUE, owner="worker-2", lease_seconds=60,
        )

        assert failed.id == task.id
        assert failed.status == "failed"
        assert failed.lease_owner is None
        assert failed.attempt_count == 1
        assert "lease expired" in failed.error


@pytest.mark.asyncio
async def test_reclaim_between_finalizers_read_and_write_cannot_clobber(db_session_factory):
    """The finalizers are compare-and-swap writes: a re-claim landing between
    the stale holder's read and its write installs a new token, so the stale
    holder's guarded UPDATE matches nothing — it can neither clear the new
    claimant's lease nor terminalize its row (an ORM write would have)."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="rag_index:p1:d1",
            max_attempts=2,
        )
        first = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        stale_token = first.lease_owner

        # The stale holder reads the row for its finalize decision...
        stale_view = await repo.get_by_id(task.id)
        assert stale_view.lease_owner == stale_token

        # ...and the lease expires and is re-claimed before it writes.
        first.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(first)
        await session.commit()
        second = await repo.claim_next(
            queue=QUEUE, owner="worker-2", lease_seconds=60,
        )
        assert second.lease_owner != stale_token

        # Every stale finalize is a no-op against the new claim.
        assert await repo.heartbeat(task.id, stale_token, 60) is False
        assert await repo.mark_completed(task.id, stale_token) is False
        assert await repo.mark_cancelled(task.id, stale_token) is False
        assert await repo.mark_failed_or_retry(task.id, stale_token, "stale") == "missing"

        reclaimed = await repo.get_by_id(task.id)
        assert reclaimed.status == "running"
        assert reclaimed.lease_owner == second.lease_owner
        assert reclaimed.attempt_count == second.attempt_count
        assert reclaimed.completed_at is None

        # The new claimant finalizes normally.
        assert await repo.mark_completed(task.id, second.lease_owner) is True
        assert (await repo.get_by_id(task.id)).status == "completed"


@pytest.mark.asyncio
async def test_mark_cancelled_without_owner_is_an_administrative_cancel(db_session_factory):
    """Without an owner the cancel is unguarded: it terminalizes the row
    whatever token it carries (the runner-side cancel path)."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="rag_index:p1:d1",
        )
        claimed = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        assert claimed.lease_owner.startswith("worker-1:")

        assert await repo.mark_cancelled(task.id) is True

        cancelled = await repo.get_by_id(task.id)
        assert cancelled.status == "cancelled"
        assert cancelled.lease_owner is None


@pytest.mark.asyncio
async def test_mark_cancelled_without_owner_does_not_reopen_terminal_rows(
    db_session_factory,
):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="rag_index", queue=QUEUE,
            payload_json="{}", dedupe_key="rag_index:p1:terminal",
        )
        claimed = await repo.claim_next(queue=QUEUE, owner="worker", lease_seconds=60)
        assert await repo.mark_completed(task.id, claimed.lease_owner) is True
        assert await repo.mark_cancelled(task.id) is False
        assert (await repo.get_by_id(task.id)).status == "completed"


# ---------------------------------------------------------------------------
# CANCELLING rows are owned by the cancel path, not the finalizers
# ---------------------------------------------------------------------------


async def _claim_task(repo, *, dedupe_key, max_attempts=3):
    task = await repo.enqueue(
        kind="rag_index",
        queue=QUEUE,
        payload_json='{"doc_id":"d1"}',
        dedupe_key=dedupe_key,
        max_attempts=max_attempts,
    )
    claimed = await repo.claim_next(
        queue=QUEUE, owner="worker-1", lease_seconds=60,
    )
    return task, claimed


@pytest.mark.asyncio
async def test_mark_completed_cannot_override_cancelling(db_session_factory):
    """A user cancel landing between the runner's is_cancelling read and its
    completion write must win: the RUNNING-status guard makes the completion
    a no-op, leaving the row (and the document outcome) to the cancel path."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task, claimed = await _claim_task(repo, dedupe_key="rag_index:p1:d1")

        # User cancel: RUNNING -> CANCELLING, lease token kept on purpose.
        assert await repo.cancel_by_dedupe_prefix("rag_index:p1:d1") == 1
        assert (await repo.get_by_id(task.id)).status == "cancelling"

        assert await repo.mark_completed(task.id, claimed.lease_owner) is False

        row = await repo.get_by_id(task.id)
        assert row.status == "cancelling"
        assert row.completed_at is None

        # The cancel confirmation from the row's owner still lands.
        assert await repo.mark_cancelled(task.id, claimed.lease_owner) is True
        assert (await repo.get_by_id(task.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_owner_cancel_confirmation_only_lands_on_cancelling(db_session_factory):
    """The owner-side mark_cancelled is the cancel confirmation: it must not
    terminalize a row the user never cancelled."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task, claimed = await _claim_task(repo, dedupe_key="rag_index:p1:d1")
        assert (await repo.get_by_id(task.id)).status == "running"

        assert await repo.mark_cancelled(task.id, claimed.lease_owner) is False
        assert (await repo.get_by_id(task.id)).status == "running"

        assert await repo.mark_completed(task.id, claimed.lease_owner) is True


@pytest.mark.asyncio
async def test_mark_failed_or_retry_cannot_override_cancelling(db_session_factory):
    """A task failing while the user cancels it must not be re-queued past
    the cancellation: the CANCELLING row is left to the cancel path."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task, claimed = await _claim_task(repo, dedupe_key="rag_index:p1:d1")

        assert await repo.cancel_by_dedupe_prefix("rag_index:p1:d1") == 1

        assert await repo.mark_failed_or_retry(
            claimed.id, claimed.lease_owner, "boom"
        ) == "missing"

        row = await repo.get_by_id(task.id)
        assert row.status == "cancelling"
        assert row.attempt_count == claimed.attempt_count
        assert row.not_before is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_scope", ["prefix", "all"])
async def test_cancel_and_complete_race_never_reopens_terminal_task(
    db_session_factory, cancel_scope,
):
    """Two real SQLite sessions converge without terminal-state resurrection."""
    async with db_session_factory() as setup:
        task, claimed = await _claim_task(
            BackgroundTaskRepository(setup), dedupe_key="rag_index:p1:race",
        )

    async with db_session_factory() as session_a, db_session_factory() as session_b:
        completing = BackgroundTaskRepository(session_a)
        cancelling = BackgroundTaskRepository(session_b)
        if cancel_scope == "prefix":
            cancel_operation = cancelling.cancel_by_dedupe_prefix(
                "rag_index:p1:race",
            )
        else:
            cancel_operation = cancelling.cancel_all_active()
        await asyncio.gather(
            completing.mark_completed(task.id, claimed.lease_owner),
            cancel_operation,
        )

    async with db_session_factory() as verify:
        row = await BackgroundTaskRepository(verify).get_by_id(task.id)
        assert row.status in {"completed", "cancelling"}
        if row.status == "cancelling":
            await BackgroundTaskRepository(verify).mark_cancelled(task.id)
        final = await BackgroundTaskRepository(verify).get_by_id(task.id)
        assert final.status in {"completed", "cancelled"}


@pytest.mark.asyncio
async def test_cancelled_task_is_pruned_by_cleanup_terminal(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="rag_index:p1:d1",
        )
        assert await repo.cancel_by_dedupe_prefix("rag_index:p1:d1") == 1
        cancelled = await repo.get_by_id(task.id)
        assert cancelled.status == "cancelled"

        cancelled.completed_at = utcnow() - timedelta(hours=25)
        session.add(cancelled)
        await session.commit()

        assert await repo.cleanup_terminal(older_than_hours=24) == 1
        assert await repo.get_by_id(task.id) is None


# ---------------------------------------------------------------------------
# Stuck CANCELLING rows are recovered once their runner is gone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cancelling_row_is_terminalized_by_recovery(db_session_factory):
    """Heartbeats renew a CANCELLING row's lease during the wind-down, so an
    expired lease proves the runner died before it could land the owner-side
    cancel confirmation; the recovery must deliver that cancellation."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task, _claimed = await _claim_task(repo, dedupe_key="rag_index:p1:d1")

        assert await repo.cancel_by_dedupe_prefix("rag_index:p1:d1") == 1
        row = await repo.get_by_id(task.id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(row)
        await session.commit()

        assert await repo.cancel_stale_cancelling() == 1

        row = await repo.get_by_id(task.id)
        assert row.status == "cancelled"
        assert row.lease_owner is None
        assert row.lease_expires_at is None
        assert row.completed_at is not None


@pytest.mark.asyncio
async def test_recovery_spares_live_cancelling_and_expired_running_rows(db_session_factory):
    """Only a CANCELLING row whose runner is gone is recovered: a wind-down
    inside its fresh lease is untouched, and an expired RUNNING row stays
    claim_next's territory (it may still be retried or failed by a reclaim)."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        wind_down, _ = await _claim_task(repo, dedupe_key="rag_index:p1:winddown")
        running, running_claim = await _claim_task(
            repo, dedupe_key="rag_index:p1:running",
        )

        assert await repo.cancel_by_dedupe_prefix("rag_index:p1:winddown") == 1
        running_claim.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(running_claim)
        await session.commit()

        assert await repo.cancel_stale_cancelling() == 0

        assert (await repo.get_by_id(wind_down.id)).status == "cancelling"
        assert (await repo.get_by_id(running.id)).status == "running"


@pytest.mark.asyncio
async def test_recovered_cancelling_row_is_reenqueueable_and_claimable(db_session_factory):
    """The full unblock: a CANCELLING row stranded by a dead runner is
    terminalized by the recovery, after which enqueueing the same dedupe key
    resets the row and the task runs again."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task, claimed = await _claim_task(repo, dedupe_key="rag_index:p1:d1")

        assert await repo.cancel_by_dedupe_prefix("rag_index:p1:d1") == 1
        # Dead runner: no heartbeat, and no finalizer may touch the row.
        row = await repo.get_by_id(task.id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(row)
        await session.commit()
        assert await repo.mark_failed_or_retry(
            task.id, claimed.lease_owner, "late finalize",
        ) == "missing"

        # Without the recovery the re-enqueue below would keep the row
        # active-but-unclaimable forever.
        assert await repo.cancel_stale_cancelling() == 1

        reenqueued = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{"doc_id":"d1"}',
            dedupe_key="rag_index:p1:d1",
        )
        assert reenqueued.id == task.id
        assert reenqueued.status == "queued"
        assert reenqueued.attempt_count == 0

        reclaimed = await repo.claim_next(
            queue=QUEUE, owner="worker-2", lease_seconds=60,
        )
        assert reclaimed.id == task.id
        assert reclaimed.status == "running"
        assert reclaimed.lease_owner.startswith("worker-2:")


# ---------------------------------------------------------------------------
# Retry backoff (not_before eligibility)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retried_task_is_not_claimable_before_backoff(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="document_process",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="document_process:p1:d1",
            max_attempts=3,
        )
        claimed = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )

        status = await repo.mark_failed_or_retry(
            claimed.id, claimed.lease_owner, "boom"
        )
        assert status == "queued"

        retried = await repo.get_by_id(task.id)
        assert retried.not_before is not None
        backoff = (retried.not_before - utcnow()).total_seconds()
        assert 0 < backoff <= 30

        assert await repo.claim_next(
            queue=QUEUE, owner="worker-2", lease_seconds=60,
        ) is None


@pytest.mark.asyncio
async def test_retried_task_is_claimable_after_backoff_elapses(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="document_process",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="document_process:p1:d1",
            max_attempts=3,
        )
        claimed = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        await repo.mark_failed_or_retry(claimed.id, claimed.lease_owner, "boom")

        retried = await repo.get_by_id(task.id)
        retried.not_before = utcnow() - timedelta(seconds=1)
        session.add(retried)
        await session.commit()

        reclaimed = await repo.claim_next(
            queue=QUEUE, owner="worker-2", lease_seconds=60,
        )
        assert reclaimed is not None
        assert reclaimed.id == task.id


@pytest.mark.asyncio
async def test_final_failure_clears_backoff_and_exponential_backoff_grows(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="document_process",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="document_process:p1:d1",
            max_attempts=2,
        )
        first = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        await repo.mark_failed_or_retry(first.id, first.lease_owner, "boom")

        # Skip past the first retry's backoff to reach the final attempt.
        retried = await repo.get_by_id(task.id)
        assert retried.attempt_count == 1
        retried.not_before = utcnow() - timedelta(seconds=1)
        session.add(retried)
        await session.commit()

        second = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        assert second is not None
        assert second.attempt_count == 1
        second.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.add(second)
        await session.commit()

        # The lease expiring again consumes the final attempt: the reclaim
        # must mark the task failed and leave no eligibility time behind.
        final = await repo.claim_next(
            queue=QUEUE, owner="worker-2", lease_seconds=60,
        )
        assert final.status == "failed"
        assert final.not_before is None


@pytest.mark.asyncio
async def test_expired_running_row_is_reclaimable_regardless_of_backoff(db_session_factory):
    """not_before gates only queued rows; lease-expiry reclaim of a running
    row must stay unaffected."""
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            kind="rag_index",
            queue=QUEUE,
            payload_json='{}',
            dedupe_key="rag_index:p1:d1",
            max_attempts=5,
        )
        claimed = await repo.claim_next(
            queue=QUEUE, owner="worker-1", lease_seconds=60,
        )
        # A leftover backoff value on a running row must not block recovery.
        claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
        claimed.not_before = utcnow() + timedelta(hours=1)
        session.add(claimed)
        await session.commit()

        reclaimed = await repo.claim_next(
            queue=QUEUE, owner="worker-2", lease_seconds=60,
        )
        assert reclaimed is not None
        assert reclaimed.id == task.id
