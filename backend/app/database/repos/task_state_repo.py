"""
TaskState Repository — CRUD operations for TaskState model.

Only this file (and other files in database/) may import TaskState directly.
All services that need task state data MUST use this repository.
"""

from typing import Optional, Dict

import json

from sqlalchemy import select, delete as sql_delete, and_, update, case
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TaskState

from app.core.logging import get_logger
from app.core.task_status import (
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
)
from app.core.utils import utcnow, parse_iso

logger = get_logger(__name__)

HEARTBEAT_STALE_SECONDS = 120  # Running task is stale if heartbeat_at is NULL or older than this
QUEUED_STALE_SECONDS = 60     # Consider queued task stale after 60s


def _utcnow_iso() -> str:
    """SQLite-compatible ISO timestamp for String-column storage.

    Uses space separator (``2026-05-28 12:00:00``) so SQLite string
    comparison with ``datetime('now', ...)`` works correctly.
    """
    return utcnow().strftime('%Y-%m-%d %H:%M:%S')


class TaskStateRepository:
    """Repository for TaskState table operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def set_queued(
        self,
        task_id: str,
        task_type: str = "llm",
        session_id: str = "",
        owner_type: str = "chat_session",
        owner_id: str = "",
        interaction_state: dict | None = None,
    ) -> None:
        """Insert a new task or reset an existing one to queued status."""
        now = _utcnow_iso()
        resolved_owner_id = owner_id or session_id or None
        interaction_state_json = (
            json.dumps(interaction_state, ensure_ascii=False)
            if interaction_state is not None else None
        )
        stmt = sqlite_insert(TaskState).values(
            task_id=task_id,
            session_id=session_id or None,
            owner_type=owner_type,
            owner_id=resolved_owner_id,
            status=STATUS_QUEUED,
            task_type=task_type,
            error=None,
            heartbeat_at=None,
            interaction_state=interaction_state_json,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["task_id"],
            set_={
                "session_id": session_id or None,
                "owner_type": owner_type,
                "owner_id": resolved_owner_id,
                "status": STATUS_QUEUED,
                "task_type": task_type,
                "error": None,
                "heartbeat_at": None,
                "interaction_state": interaction_state_json,
                "created_at": now,
                "updated_at": now,
            },
        )
        await self._session.execute(stmt)
        await self._session.commit()  # Required: raw execute() doesn't auto-commit in async sessions

    async def heartbeat(self, task_id: str) -> None:
        """Refresh the heartbeat for runnable worker-owned tasks.

        A guarded, cancel-aware UPDATE: it promotes a queued task to running
        and refreshes a running task, but never touches a cancelling task.
        Leaving cancelling alone is deliberate: it freezes ``heartbeat_at`` at
        the last pre-cancel value so that, if the worker gets stuck inside a
        non-cooperative operation that keeps the heartbeat loop alive, the task
        still ages into staleness and recovers via the stale-grace path rather
        than hanging on ``cancelling`` forever. The status predicate in the
        WHERE clause also makes this safe against a concurrent cancel write
        from the web process, which the previous read-then-mutate form was not.
        """
        now = _utcnow_iso()
        await self._session.execute(
            update(TaskState)
            .where(
                TaskState.task_id == task_id,
                TaskState.status.in_((STATUS_QUEUED, STATUS_RUNNING)),
            )
            .values(
                status=case(
                    (TaskState.status == STATUS_QUEUED, STATUS_RUNNING),
                    else_=TaskState.status,
                ),
                heartbeat_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()

    async def mark_completed(self, task_id: str) -> None:
        """Mark a task completed, honoring a cancel requested mid-run.

        Guarded UPDATE: a cancelling task finalizes as cancelled (the user's
        intent outranks a clean completion); tasks paused for input or already
        terminal are left untouched.
        """
        now = _utcnow_iso()
        await self._session.execute(
            update(TaskState)
            .where(
                TaskState.task_id == task_id,
                TaskState.status.in_((STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING)),
            )
            .values(
                status=case(
                    (TaskState.status == STATUS_CANCELLING, STATUS_CANCELLED),
                    else_=STATUS_COMPLETED,
                ),
                heartbeat_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()

    async def mark_failed(self, task_id: str, error: str) -> None:
        """Mark a task failed, honoring a cancel requested mid-run.

        Guarded UPDATE: a cancelling task finalizes as cancelled with no error
        surfaced, since the failure is a side effect of winding down. The WHERE
        predicate also protects terminal/input-paused tasks from a stale late
        write (the previous unguarded form could clobber any status).
        """
        now = _utcnow_iso()
        await self._session.execute(
            update(TaskState)
            .where(
                TaskState.task_id == task_id,
                TaskState.status.in_((STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING)),
            )
            .values(
                status=case(
                    (TaskState.status == STATUS_CANCELLING, STATUS_CANCELLED),
                    else_=STATUS_FAILED,
                ),
                error=case(
                    (TaskState.status == STATUS_CANCELLING, None),
                    else_=error,
                ),
                heartbeat_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()

    async def mark_cancelled(self, task_id: str) -> None:
        """Finalize a cancelling task as cancelled.

        Used by the worker when it observes the task was already cancelled
        before it could start running. Only transitions out of the cancelling
        state and clears any parked interaction state.
        """
        now = _utcnow_iso()
        await self._session.execute(
            update(TaskState)
            .where(
                TaskState.task_id == task_id,
                TaskState.status == STATUS_CANCELLING,
            )
            .values(
                status=STATUS_CANCELLED,
                interaction_state=None,
                heartbeat_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()

    async def request_cancel(self, task_id: str) -> str:
        """Record cancel intent and return the resulting effective status.

        Two guarded UPDATEs committed together:
          * queued/running -> cancelling: the worker will wind the current
            step down and finalize as cancelled.
          * awaiting_input -> cancelled: the worker has already exited, so go
            straight to terminal and clear the parked interaction state.

        Idempotent: re-cancelling an already cancelling or terminal task
        returns its current status with no side effect. Returns ``"not_found"``
        if no such task exists.
        """
        now = _utcnow_iso()
        update_result = await self._session.execute(
            update(TaskState)
            .where(
                TaskState.task_id == task_id,
                TaskState.status.in_((STATUS_QUEUED, STATUS_RUNNING)),
            )
            .values(status=STATUS_CANCELLING, updated_at=now)
        )
        if update_result.rowcount == 0:
            await self._session.execute(
                update(TaskState)
                .where(
                    TaskState.task_id == task_id,
                    TaskState.status == STATUS_AWAITING_INPUT,
                )
                .values(
                    status=STATUS_CANCELLED,
                    interaction_state=None,
                    heartbeat_at=now,
                    updated_at=now,
                )
            )
        await self._session.commit()

        status_result = await self._session.execute(
            select(TaskState.status).where(TaskState.task_id == task_id)
        )
        status = status_result.scalar_one_or_none()
        return status if status is not None else "not_found"

    async def is_cancelling(self, task_id: str) -> bool:
        """Return True if the task is in the cancelling state."""
        result = await self._session.execute(
            select(TaskState.status).where(TaskState.task_id == task_id)
        )
        return result.scalar_one_or_none() == STATUS_CANCELLING

    async def get_status(self, task_id: str) -> Optional[str]:
        """Return the raw persisted status for a task, or None if no row exists.

        Unlike ``check_liveness``, this does not derive staleness, so it is the
        right read for deciding whether a dequeued worker job should run: a
        long-queued task that liveness would call ``stale`` is still legitimately
        ``queued`` here and must not be skipped.
        """
        result = await self._session.execute(
            select(TaskState.status).where(TaskState.task_id == task_id)
        )
        return result.scalar_one_or_none()

    async def mark_awaiting_input(self, task_id: str, interaction_state: dict) -> None:
        """Park a task to await user input, unless it was cancelled mid-run.

        Guarded UPDATE: only transitions from queued/running, so a cancel that
        lands while an interactive tool is preparing its pause is honored
        rather than overwritten back to awaiting_input.
        """
        now = _utcnow_iso()
        await self._session.execute(
            update(TaskState)
            .where(
                TaskState.task_id == task_id,
                TaskState.status.in_((STATUS_QUEUED, STATUS_RUNNING)),
            )
            .values(
                status=STATUS_AWAITING_INPUT,
                interaction_state=json.dumps(interaction_state, ensure_ascii=False),
                heartbeat_at=now,
                updated_at=now,
            )
        )
        await self._session.commit()

    async def clear_interaction_by_session(self, session_id: str) -> None:
        """Clear interaction state for all awaiting_input tasks in a session."""
        now = _utcnow_iso()
        result = await self._session.execute(
            select(TaskState).where(
                TaskState.session_id == session_id,
                TaskState.status == STATUS_AWAITING_INPUT,
            )
        )
        tasks = list(result.scalars().all())
        if tasks:
            for task in tasks:
                task.status = STATUS_COMPLETED
                task.interaction_state = None
                task.heartbeat_at = now
                task.updated_at = now
            await self._session.commit()

    async def delete_by_session(self, session_id: str) -> int:
        """Delete all task state records for a session."""
        result = await self._session.execute(
            sql_delete(TaskState).where(TaskState.session_id == session_id)
        )
        await self._session.commit()
        return result.rowcount

    async def get_pending_interaction_by_session(
        self, session_id: str
    ) -> Optional[dict]:
        """Return the latest pending interaction checkpoint for one chat session.

        A checkpoint is a row whose ``status='awaiting_input'`` and whose
        ``interaction_state`` column is non-null. This is intentionally
        session-scoped: annotation tasks carry ``session_id=NULL`` by design
        and are excluded — chat callers never receive annotation state.

        Status violations (e.g. a row with non-null ``interaction_state``
        but ``status='completed'``) are treated as invariant violations
        and surfaced as "not found", not silently recovered. The write
        side owns state-machine correctness.
        """
        if not session_id:
            return None
        result = await self._session.execute(
            select(TaskState)
            .where(
                TaskState.session_id == session_id,
                TaskState.status == STATUS_AWAITING_INPUT,
                TaskState.interaction_state.isnot(None),
            )
            .order_by(TaskState.created_at.desc())
            .limit(1)
        )
        task = result.scalar_one_or_none()
        if not task:
            return None
        return self._parse_json(task.interaction_state)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_active_by_session(self, session_id: str) -> Optional[Dict]:
        """Return the latest active task for a session.

        Active here spans runnable and paused states: queued, running,
        awaiting_input, and cancelling (wind-down in progress).
        """
        return await self.get_active_by_owner("chat_session", session_id)

    async def get_active_annotation_reply(self, annotation_id: str) -> Optional[Dict]:
        """Return the latest active annotation reply task for an annotation."""
        return await self.get_active_by_owner("annotation", annotation_id)

    async def get_active_by_owner(self, owner_type: str, owner_id: str) -> Optional[Dict]:
        """Return latest active task for a logical owner."""
        if not owner_type or not owner_id:
            return None
        result = await self._session.execute(
            select(TaskState)
            .where(
                and_(
                    TaskState.owner_type == owner_type,
                    TaskState.owner_id == owner_id,
                    TaskState.status.in_([
                        STATUS_QUEUED, STATUS_RUNNING,
                        STATUS_AWAITING_INPUT, STATUS_CANCELLING,
                    ]),
                )
            )
            .order_by(TaskState.created_at.desc())
            .limit(1)
        )
        task = result.scalar_one_or_none()
        return self._to_dict(task) if task else None

    async def reap_stale_tasks(self) -> int:
        """Finalize chat tasks whose worker died without a clean exit.

        Finds tasks still in ``running`` or ``cancelling`` whose heartbeat has
        expired (per ``check_liveness``) and marks them failed. ``mark_failed``
        is cancel-aware — a stale ``cancelling`` task finalizes as ``cancelled``.

        This is the only stale-recovery path that runs independently of an
        active SSE subscriber, so a task orphaned by a worker crash is cleaned
        up even when the user has navigated away.
        """
        result = await self._session.execute(
            select(TaskState).where(
                TaskState.status.in_([STATUS_RUNNING, STATUS_CANCELLING])
            )
        )
        reaped = 0
        for task in result.scalars():
            liveness = await self.check_liveness(task.task_id)
            if liveness == "stale":
                await self.mark_failed(
                    task.task_id,
                    "Worker stopped sending heartbeats and did not recover.",
                )
                reaped += 1
        return reaped

    async def get_by_id(self, task_id: str) -> Optional[Dict]:
        """Return a task state dict by task_id, or None."""
        result = await self._session.execute(
            select(TaskState).where(TaskState.task_id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task:
            return None
        return self._to_dict(task)

    def _to_dict(self, task: TaskState) -> Dict:
        return {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "owner_type": task.owner_type,
            "owner_id": task.owner_id,
            "status": task.status,
            "task_type": task.task_type,
            "error": task.error,
            "heartbeat_at": task.heartbeat_at,
            "interaction_state": self._parse_json(task.interaction_state),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    async def check_liveness(self, task_id: str) -> str:
        """
        Return the task's effective status:
        'not_found'      -- no record
        'queued'         -- waiting for worker
        'running'        -- worker is alive (heartbeat fresh)
        'cancelling'     -- cancel requested, worker winding down (heartbeat fresh)
        'stale'          -- was running/cancelling but heartbeat is stale (worker likely dead)
        'awaiting_input' -- worker paused, waiting for user feedback
        'completed'      -- finished successfully
        'failed'         -- finished with error
        'cancelled'      -- finished by user cancellation
        """
        result = await self._session.execute(
            select(TaskState).where(TaskState.task_id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task:
            return "not_found"

        if task.status in (STATUS_COMPLETED, STATUS_FAILED, STATUS_AWAITING_INPUT, STATUS_CANCELLED):
            return task.status

        if task.status == STATUS_QUEUED:
            # If queued for > 60 seconds without being picked up, it's stale
            try:
                created = parse_iso(task.created_at)
                age = (utcnow() - created).total_seconds()
                if age > QUEUED_STALE_SECONDS:
                    return "stale"
            except (ValueError, TypeError):
                return "stale"
            return STATUS_QUEUED

        if task.status == STATUS_CANCELLING:
            # The worker is winding down. heartbeat() deliberately stops
            # refreshing a cancelling task, so heartbeat_at is frozen at the
            # last pre-cancel value and ages into staleness on the same bound as
            # a running task — that is what bounds a stuck non-cooperative
            # wind-down. If the task was cancelled before the worker ever
            # heartbeated, age it from created_at (like queued) so a pending Huey
            # job still has a bounded window to observe the cancel rather than
            # the lock releasing instantly.
            if not task.heartbeat_at:
                try:
                    age = (utcnow() - parse_iso(task.created_at)).total_seconds()
                    if age > QUEUED_STALE_SECONDS:
                        return "stale"
                except (ValueError, TypeError):
                    return "stale"
                return STATUS_CANCELLING
            try:
                age = (utcnow() - parse_iso(task.heartbeat_at)).total_seconds()
                if age > HEARTBEAT_STALE_SECONDS:
                    return "stale"
            except (ValueError, TypeError):
                return "stale"
            return STATUS_CANCELLING

        # Status is 'running' -- check heartbeat freshness
        if not task.heartbeat_at:
            return "stale"
        try:
            hb = parse_iso(task.heartbeat_at)
            age = (utcnow() - hb).total_seconds()
            if age > HEARTBEAT_STALE_SECONDS:
                return "stale"
        except (ValueError, TypeError):
            return "stale"

        return "running"

    @staticmethod
    def _parse_json(value: Optional[str]) -> Optional[dict]:
        if not value:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
