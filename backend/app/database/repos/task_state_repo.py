"""
TaskState Repository — CRUD operations for TaskState model.

Only this file (and other files in database/) may import TaskState directly.
All services that need task state data MUST use this repository.
"""

from typing import Optional, Dict

import json

from sqlalchemy import select, delete as sql_delete, and_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import TaskState

from app.core.logging import get_logger
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
            status="queued",
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
                "status": "queued",
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
        """Update heartbeat timestamp for active worker-owned states.

        Heartbeats are emitted independently from the stream loop. They must not
        overwrite a task that has paused for user input or already reached a
        terminal state.
        """
        now = _utcnow_iso()
        result = await self._session.execute(
            select(TaskState).where(TaskState.task_id == task_id)
        )
        task = result.scalar_one_or_none()
        if task:
            if task.status in ("awaiting_input", "completed", "failed"):
                return
            task.status = "running"
            task.heartbeat_at = now
            task.updated_at = now
            await self._session.commit()

    async def mark_completed(self, task_id: str) -> None:
        """Mark a task as completed."""
        now = _utcnow_iso()
        result = await self._session.execute(
            select(TaskState).where(TaskState.task_id == task_id)
        )
        task = result.scalar_one_or_none()
        if task:
            task.status = "completed"
            task.heartbeat_at = now
            task.updated_at = now
            await self._session.commit()

    async def mark_failed(self, task_id: str, error: str) -> None:
        """Mark a task as failed with an error message."""
        now = _utcnow_iso()
        result = await self._session.execute(
            select(TaskState).where(TaskState.task_id == task_id)
        )
        task = result.scalar_one_or_none()
        if task:
            task.status = "failed"
            task.error = error
            task.heartbeat_at = now
            task.updated_at = now
            await self._session.commit()

    async def mark_awaiting_input(self, task_id: str, interaction_state: dict) -> None:
        """Mark task as waiting for user input. Survives indefinitely (never GC'd)."""
        now = _utcnow_iso()
        result = await self._session.execute(
            select(TaskState).where(TaskState.task_id == task_id)
        )
        task = result.scalar_one_or_none()
        if task:
            task.status = "awaiting_input"
            task.interaction_state = json.dumps(interaction_state, ensure_ascii=False)
            task.heartbeat_at = now
            task.updated_at = now
            await self._session.commit()
            logger.debug("mark_awaiting_input: task_id=%s status=%s", task_id, task.status)

    async def clear_interaction_by_session(self, session_id: str) -> None:
        """Clear interaction state for all awaiting_input tasks in a session."""
        now = _utcnow_iso()
        result = await self._session.execute(
            select(TaskState).where(
                TaskState.session_id == session_id,
                TaskState.status == "awaiting_input",
            )
        )
        tasks = list(result.scalars().all())
        if tasks:
            for task in tasks:
                task.status = "completed"
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
                TaskState.status == "awaiting_input",
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
        """Return the latest active (queued, running, or awaiting_input) task for a session."""
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
                    TaskState.status.in_(["queued", "running", "awaiting_input"]),
                )
            )
            .order_by(TaskState.created_at.desc())
            .limit(1)
        )
        task = result.scalar_one_or_none()
        return self._to_dict(task) if task else None

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
        'not_found'     -- no record
        'queued'        -- waiting for worker
        'running'       -- worker is alive (heartbeat fresh)
        'stale'         -- was running but heartbeat is stale (worker likely dead)
        'awaiting_input' -- worker paused, waiting for user feedback
        'completed'     -- finished successfully
        'failed'        -- finished with error
        """
        result = await self._session.execute(
            select(TaskState).where(TaskState.task_id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task:
            return "not_found"

        if task.status in ("completed", "failed", "awaiting_input"):
            return task.status

        if task.status == "queued":
            # If queued for > 60 seconds without being picked up, it's stale
            try:
                created = parse_iso(task.created_at)
                age = (utcnow() - created).total_seconds()
                if age > QUEUED_STALE_SECONDS:
                    return "stale"
            except (ValueError, TypeError):
                return "stale"
            return "queued"

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
