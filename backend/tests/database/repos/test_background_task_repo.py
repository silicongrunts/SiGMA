from datetime import timedelta

import pytest

from app.core.utils import utcnow
from app.database.repos.background_task_repo import BackgroundTaskRepository

QUEUE = "library"


@pytest.mark.asyncio
async def test_enqueue_dedupes_active_task(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        first = await repo.enqueue(
            project_id="p1",
            kind="rag_index",
            queue=QUEUE,
            payload_json='{"doc_id":"d1"}',
            dedupe_key="rag_index:p1:d1",
            priority=100,
        )
        second = await repo.enqueue(
            project_id="p1",
            kind="rag_index",
            queue=QUEUE,
            payload_json='{"doc_id":"d1","again":true}',
            dedupe_key="rag_index:p1:d1",
            priority=50,
        )

        assert second.id == first.id
        assert second.priority == 50
        assert second.payload_json == '{"doc_id":"d1","again":true}'


@pytest.mark.asyncio
async def test_claim_heartbeat_and_complete(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        queued = await repo.enqueue(
            project_id="p1",
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
        assert await repo.heartbeat(claimed.id, "worker-1", 60) is True

        await repo.mark_completed(claimed.id, "worker-1")
        done = await repo.get_by_id(claimed.id)
        assert done.status == "completed"
        assert done.lease_owner is None


@pytest.mark.asyncio
async def test_expired_running_task_is_reclaimed_and_counts_attempt(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            project_id="p1",
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
        assert reclaimed.lease_owner == "worker-2"
        assert reclaimed.attempt_count == 1


@pytest.mark.asyncio
async def test_expired_running_task_returns_failed_when_retry_budget_exhausted(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            project_id="p1",
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
async def test_cancel_and_cleanup_terminal_tasks(db_session_factory):
    async with db_session_factory() as session:
        repo = BackgroundTaskRepository(session)
        task = await repo.enqueue(
            project_id="p1",
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
