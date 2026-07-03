"""Repository for durable background task queue entries."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import generate_id, utcnow
from app.database.models import BackgroundTask


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_STATUSES = {"queued", "running", "cancelling"}


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
        project_id: str,
        kind: str,
        queue: str,
        payload_json: str,
        priority: int = 100,
        max_attempts: int = 3,
        dedupe_key: Optional[str] = None,
    ) -> BackgroundTask:
        task_id = _task_id_for_dedupe(dedupe_key) if dedupe_key else generate_id()
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
            task.project_id = project_id
            task.kind = kind
            task.queue = queue
            task.status = "queued"
            task.priority = priority
            task.payload_json = payload_json
            task.dedupe_key = dedupe_key
            task.attempt_count = 0
            task.max_attempts = max_attempts
            task.lease_owner = None
            task.lease_expires_at = None
            task.heartbeat_at = None
            task.error = None
            task.created_at = now
            task.updated_at = now
            task.started_at = None
            task.completed_at = None
        else:
            task = BackgroundTask(
                id=task_id,
                project_id=project_id,
                kind=kind,
                queue=queue,
                status="queued",
                priority=priority,
                payload_json=payload_json,
                dedupe_key=dedupe_key,
                max_attempts=max_attempts,
            )
        self._session.add(task)
        await self._session.commit()
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
        while True:
            now = utcnow()
            result = await self._session.execute(
                select(BackgroundTask)
                .where(
                    BackgroundTask.queue == queue,
                    or_(
                        BackgroundTask.status == "queued",
                        and_(
                            BackgroundTask.status == "running",
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

            claim_conditions = [BackgroundTask.id == task.id]
            if task.status == "running":
                claim_conditions.extend([
                    BackgroundTask.status == "running",
                    BackgroundTask.lease_expires_at <= now,
                ])
                next_attempt = task.attempt_count + 1
                if next_attempt >= task.max_attempts:
                    result = await self._session.execute(
                        update(BackgroundTask)
                        .where(*claim_conditions)
                        .values(
                            status="failed",
                            attempt_count=next_attempt,
                            error="Task lease expired too many times; marking failed",
                            lease_owner=None,
                            lease_expires_at=None,
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
                claim_conditions.append(BackgroundTask.status == "queued")
                next_attempt = task.attempt_count

            result = await self._session.execute(
                update(BackgroundTask)
                .where(*claim_conditions)
                .values(
                    status="running",
                    attempt_count=next_attempt,
                    lease_owner=owner,
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
        task = await self.get_by_id(task_id)
        if not task or task.lease_owner != owner:
            return False
        if task.status not in {"running", "cancelling"}:
            return False
        now = utcnow()
        task.heartbeat_at = now
        task.lease_expires_at = now + timedelta(seconds=lease_seconds)
        task.updated_at = now
        self._session.add(task)
        await self._session.commit()
        return True

    async def is_cancelling(self, task_id: str) -> bool:
        task = await self.get_by_id(task_id)
        return bool(task and task.status == "cancelling")

    async def mark_completed(self, task_id: str, owner: str) -> None:
        task = await self.get_by_id(task_id)
        if not task or task.lease_owner != owner:
            return
        now = utcnow()
        task.status = "completed"
        task.lease_owner = None
        task.lease_expires_at = None
        task.heartbeat_at = now
        task.completed_at = now
        task.updated_at = now
        task.error = None
        self._session.add(task)
        await self._session.commit()

    async def mark_cancelled(self, task_id: str, owner: str | None = None) -> None:
        task = await self.get_by_id(task_id)
        if not task or (owner and task.lease_owner != owner):
            return
        now = utcnow()
        task.status = "cancelled"
        task.lease_owner = None
        task.lease_expires_at = None
        task.heartbeat_at = now
        task.completed_at = now
        task.updated_at = now
        self._session.add(task)
        await self._session.commit()

    async def mark_failed_or_retry(self, task_id: str, owner: str, error: str) -> str:
        task = await self.get_by_id(task_id)
        if not task or task.lease_owner != owner:
            return "missing"
        task.attempt_count += 1
        task.error = error[:4000]
        task.lease_owner = None
        task.lease_expires_at = None
        task.heartbeat_at = utcnow()
        task.updated_at = utcnow()
        if task.attempt_count >= task.max_attempts:
            task.status = "failed"
            task.completed_at = utcnow()
        else:
            task.status = "queued"
        status = task.status
        self._session.add(task)
        await self._session.commit()
        return status

    async def cancel_by_dedupe_prefix(self, prefix: str) -> int:
        result = await self._session.execute(
            select(BackgroundTask).where(
                BackgroundTask.dedupe_key.is_not(None),
                BackgroundTask.dedupe_key.startswith(prefix),
                BackgroundTask.status.in_(list(ACTIVE_STATUSES)),
            )
        )
        tasks = list(result.scalars().all())
        for task in tasks:
            task.status = "cancelling" if task.status == "running" else "cancelled"
            task.updated_at = utcnow()
            if task.status == "cancelled":
                task.completed_at = utcnow()
        if tasks:
            await self._session.commit()
        return len(tasks)

    async def cancel_all_active(self) -> int:
        result = await self._session.execute(
            select(BackgroundTask).where(BackgroundTask.status.in_(list(ACTIVE_STATUSES)))
        )
        tasks = list(result.scalars().all())
        now = utcnow()
        for task in tasks:
            task.status = "cancelling" if task.status == "running" else "cancelled"
            task.updated_at = now
            if task.status == "cancelled":
                task.completed_at = now
        if tasks:
            await self._session.commit()
        return len(tasks)

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
