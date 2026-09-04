"""Repository for durable background task queue entries."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.task_status import (
    ACTIVE_STATUSES,
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
)
from app.core.utils import generate_id, utcnow
from app.database.models import BackgroundTask


# Retry backoff before a re-queued task becomes claimable again:
# 30s, 60s, 120s, ... capped at one hour.
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_MAX_SECONDS = 3600


class BackgroundTaskRepository:
    """Persistent task queue operations.

    The queue is intentionally simple: deterministic IDs for deduped tasks,
    leasing for crash recovery, and explicit terminal cleanup.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def enqueue(
        self,
        *,
        kind: str,
        queue: str,
        payload_json: str,
        priority: int = 100,
        max_attempts: int = 3,
        dedupe_key: Optional[str] = None,
    ) -> BackgroundTask:
        """Insert or merge a task; idempotent under concurrent enqueues.

        A dedupe key maps to a deterministic primary key, so two concurrent
        enqueues of the same key can both read the row as missing and race
        the INSERT. The loser's commit fails on the unique id; after a
        rollback it re-reads the winner's committed row and lands on the
        ordinary existing-row paths below — the race loser is therefore just
        a merge (active row) or reset (terminal row) update, with the same
        merge rules as a non-raced enqueue.
        """
        task_id = _task_id_for_dedupe(dedupe_key) if dedupe_key else generate_id()
        while True:
            task = await self.get_by_id(task_id)
            now = utcnow()

            if task and task.status in ACTIVE_STATUSES:
                task.kind = kind
                task.queue = queue
                task.payload_json = payload_json
                task.priority = min(task.priority, priority)
                task.max_attempts = max(task.max_attempts, max_attempts)
                task.updated_at = now
                self._session.add(task)
                await self._session.commit()
                await self._session.refresh(task)
                return task

            if task:
                task.kind = kind
                task.queue = queue
                task.status = STATUS_QUEUED
                task.priority = priority
                task.payload_json = payload_json
                task.dedupe_key = dedupe_key
                task.attempt_count = 0
                task.max_attempts = max_attempts
                task.lease_owner = None
                task.lease_expires_at = None
                task.heartbeat_at = None
                task.not_before = None
                task.error = None
                task.created_at = now
                task.updated_at = now
                task.started_at = None
                task.completed_at = None
            else:
                task = BackgroundTask(
                    id=task_id,
                    kind=kind,
                    queue=queue,
                    status=STATUS_QUEUED,
                    priority=priority,
                    payload_json=payload_json,
                    dedupe_key=dedupe_key,
                    max_attempts=max_attempts,
                )
            self._session.add(task)
            try:
                await self._session.commit()
            except IntegrityError:
                # Lost the deterministic-id insert race. The session is in a
                # failed-transaction state and the read above predates the
                # winner's commit, so roll back first: the re-read at the top
                # of the loop then observes the winner's row.
                await self._session.rollback()
                continue
            await self._session.refresh(task)
            return task

    async def get_by_id(self, task_id: str) -> Optional[BackgroundTask]:
        result = await self._session.execute(
            select(BackgroundTask).where(BackgroundTask.id == task_id)
        )
        return result.scalar_one_or_none()

    async def claim_next(
        self,
        *,
        queue: str,
        owner: str,
        lease_seconds: int,
    ) -> Optional[BackgroundTask]:
        """Claim the next runnable task and return the refreshed row.

        Each successful claim writes a one-time lease token
        ``f"{owner}:{random}"`` into ``lease_owner``. The refreshed row's
        ``lease_owner`` is the only identity allowed to heartbeat and
        finalize the task, so re-claiming an expired lease invalidates the
        previous holder even when both claims come from the same runner.

        Queued rows are claimable only past their ``not_before`` retry
        backoff; expired RUNNING rows are always reclaimable.
        """
        while True:
            now = utcnow()
            result = await self._session.execute(
                select(BackgroundTask)
                .where(
                    BackgroundTask.queue == queue,
                    or_(
                        and_(
                            BackgroundTask.status == STATUS_QUEUED,
                            or_(
                                BackgroundTask.not_before.is_(None),
                                BackgroundTask.not_before <= now,
                            ),
                        ),
                        and_(
                            BackgroundTask.status == STATUS_RUNNING,
                            BackgroundTask.lease_expires_at <= now,
                        ),
                    ),
                )
                .order_by(BackgroundTask.priority.asc(), BackgroundTask.created_at.asc())
                .limit(1)
            )
            task = result.scalar_one_or_none()
            if not task:
                return None

            claim_token = f"{owner}:{uuid4().hex[:12]}"
            claim_conditions = [BackgroundTask.id == task.id]
            if task.status == STATUS_RUNNING:
                claim_conditions.extend([
                    BackgroundTask.status == STATUS_RUNNING,
                    BackgroundTask.lease_expires_at <= now,
                ])
                next_attempt = task.attempt_count + 1
                if next_attempt >= task.max_attempts:
                    result = await self._session.execute(
                        update(BackgroundTask)
                        .where(*claim_conditions)
                        .values(
                            status=STATUS_FAILED,
                            attempt_count=next_attempt,
                            error="Task lease expired too many times; marking failed",
                            lease_owner=None,
                            lease_expires_at=None,
                            not_before=None,
                            completed_at=now,
                            updated_at=now,
                        )
                    )
                    await self._session.commit()
                    if result.rowcount:
                        await self._session.refresh(task)
                        return task
                    continue
            else:
                claim_conditions.append(BackgroundTask.status == STATUS_QUEUED)
                next_attempt = task.attempt_count

            result = await self._session.execute(
                update(BackgroundTask)
                .where(*claim_conditions)
                .values(
                    status=STATUS_RUNNING,
                    attempt_count=next_attempt,
                    lease_owner=claim_token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    heartbeat_at=now,
                    started_at=task.started_at or now,
                    updated_at=now,
                )
            )
            await self._session.commit()
            if not result.rowcount:
                continue
            await self._session.refresh(task)
            return task

    async def heartbeat(self, task_id: str, owner: str, lease_seconds: int) -> bool:
        """Renew the lease and return whether the row matched.

        The token equality in the WHERE clause makes the renewal a
        compare-and-swap: a re-claim that lands mid-flight installs a new
        token, so the stale holder's UPDATE matches nothing instead of
        extending a lease it no longer owns.
        """
        now = utcnow()
        result = await self._session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.lease_owner == owner,
                BackgroundTask.status.in_([STATUS_RUNNING, STATUS_CANCELLING]),
            )
            .values(
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def is_cancelling(self, task_id: str) -> bool:
        task = await self.get_by_id(task_id)
        return bool(task and task.status == STATUS_CANCELLING)

    async def mark_completed(self, task_id: str, owner: str) -> bool:
        """Terminalize the RUNNING claim as completed; return whether the row matched.

        Compare-and-swap on the claim token plus the status: a stale holder's
        finalize matches nothing once the lease was re-claimed, and a row the
        user flipped to CANCELLING between the runner's cancel check and this
        write is left to the cancel path instead of being overwritten.
        """
        now = utcnow()
        result = await self._session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.lease_owner == owner,
                BackgroundTask.status == STATUS_RUNNING,
            )
            .values(
                status=STATUS_COMPLETED,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=now,
                completed_at=now,
                updated_at=now,
                error=None,
            )
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def mark_cancelled(self, task_id: str, owner: str | None = None) -> bool:
        """Terminalize the claim as cancelled; return whether the row matched.

        With *owner* this is the cancel confirmation: it lands only on the
        CANCELLING row the owner still holds, so it can neither cancel a
        QUEUED/RUNNING row the owner never saw flagged nor rewrite a terminal
        row. Without *owner* (administrative cancel) any matching row is
        cancelled.
        """
        now = utcnow()
        conditions = [
            BackgroundTask.id == task_id,
            BackgroundTask.status.in_(list(ACTIVE_STATUSES)),
        ]
        if owner is not None:
            conditions.extend([
                BackgroundTask.lease_owner == owner,
                BackgroundTask.status == STATUS_CANCELLING,
            ])
        result = await self._session.execute(
            update(BackgroundTask)
            .where(*conditions)
            .values(
                status=STATUS_CANCELLED,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=now,
                completed_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def mark_failed_or_retry(self, task_id: str, owner: str, error: str) -> str:
        """Consume one attempt of the RUNNING claim; return the new status or ``"missing"``.

        The branch (retry vs final failure) is computed from the row read,
        but the write itself is a compare-and-swap on the claim token and the
        RUNNING status: if a re-claim landed between the read and the write,
        or the user flipped the row to CANCELLING, the stale holder matches
        nothing and the row is left to its new owner. Token equality and a
        RUNNING row at write time prove neither happened, so the computed
        branch is the branch that landed. A CANCELLING row therefore keeps
        its cancel outcome instead of being re-queued past the cancellation.
        """
        task = await self.get_by_id(task_id)
        if not task or task.lease_owner != owner:
            return "missing"

        now = utcnow()
        next_attempt = task.attempt_count + 1
        values = {
            "attempt_count": next_attempt,
            "error": error[:4000],
            "lease_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": now,
            "updated_at": now,
        }
        if next_attempt >= task.max_attempts:
            status = STATUS_FAILED
            values.update(
                status=status,
                completed_at=now,
                not_before=None,
            )
        else:
            status = STATUS_QUEUED
            backoff = min(
                RETRY_BACKOFF_BASE_SECONDS * 2 ** (next_attempt - 1),
                RETRY_BACKOFF_MAX_SECONDS,
            )
            values.update(
                status=status,
                not_before=now + timedelta(seconds=backoff),
            )
        result = await self._session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.lease_owner == owner,
                BackgroundTask.status == STATUS_RUNNING,
            )
            .values(**values)
        )
        await self._session.commit()
        if not result.rowcount:
            return "missing"
        return status

    async def cancel_stale_cancelling(self) -> int:
        """Confirm the cancel of CANCELLING rows whose runner is gone; return the row count.

        Heartbeats renew a CANCELLING row's lease during the handler's
        wind-down, so an expired lease proves the runner died before it
        could land the owner-side cancel confirmation. Left alone such a
        row is stranded forever: it is never claimed, ``enqueue`` keeps
        treating it as active instead of resetting it, and terminal
        cleanup ignores it — which also blocks re-enqueueing the same
        dedupe key. The CAS on status plus expiry leaves a live wind-down
        (fresh lease) untouched.
        """
        now = utcnow()
        result = await self._session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.status == STATUS_CANCELLING,
                BackgroundTask.lease_expires_at <= now,
            )
            .values(
                status=STATUS_CANCELLED,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=now,
                completed_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()
        return result.rowcount

    async def has_inflight(self, queue: str) -> bool:
        """True while any task for *queue* still has a live handler.

        CANCELLING counts: its handler has not finalized the row yet and
        may still be writing shared state. QUEUED rows are excluded —
        they have not started, and enqueue dedupe re-activates them.
        """
        result = await self._session.execute(
            select(BackgroundTask.id).where(
                BackgroundTask.queue == queue,
                BackgroundTask.status.in_([STATUS_RUNNING, STATUS_CANCELLING]),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def has_inflight_for_dedupe_prefixes(self, prefixes: tuple[str, ...]) -> bool:
        """Return true while a live handler owns any matching task."""
        if not prefixes:
            return False
        result = await self._session.execute(
            select(BackgroundTask.id).where(
                BackgroundTask.dedupe_key.is_not(None),
                or_(*(BackgroundTask.dedupe_key.startswith(prefix) for prefix in prefixes)),
                BackgroundTask.status.in_([STATUS_RUNNING, STATUS_CANCELLING]),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def cancel_by_dedupe_prefix(self, prefix: str) -> int:
        now = utcnow()
        running = await self._session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.dedupe_key.is_not(None),
                BackgroundTask.dedupe_key.startswith(prefix),
                BackgroundTask.status == STATUS_RUNNING,
            )
            .values(status=STATUS_CANCELLING, updated_at=now)
        )
        queued = await self._session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.dedupe_key.is_not(None),
                BackgroundTask.dedupe_key.startswith(prefix),
                BackgroundTask.status == STATUS_QUEUED,
            )
            .values(
                status=STATUS_CANCELLED,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()
        return running.rowcount + queued.rowcount

    async def cancel_all_active(self) -> int:
        now = utcnow()
        running = await self._session.execute(
            update(BackgroundTask)
            .where(BackgroundTask.status == STATUS_RUNNING)
            .values(status=STATUS_CANCELLING, updated_at=now)
        )
        queued = await self._session.execute(
            update(BackgroundTask)
            .where(BackgroundTask.status == STATUS_QUEUED)
            .values(
                status=STATUS_CANCELLED,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()
        return running.rowcount + queued.rowcount

    async def cleanup_terminal(self, older_than_hours: int) -> int:
        cutoff = utcnow() - timedelta(hours=older_than_hours)
        result = await self._session.execute(
            select(BackgroundTask).where(
                BackgroundTask.status.in_(list(TERMINAL_STATUSES)),
                BackgroundTask.completed_at.is_not(None),
                BackgroundTask.completed_at < cutoff,
            )
        )
        tasks = list(result.scalars().all())
        for task in tasks:
            await self._session.delete(task)
        if tasks:
            await self._session.commit()
        return len(tasks)


def _task_id_for_dedupe(dedupe_key: str) -> str:
    import hashlib

    digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]
    return f"bg_{digest}"
