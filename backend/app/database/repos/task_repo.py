"""
Task Repository — CRUD for Task model.

Only this file (and other files in database/) may import Task directly.
Services use this repository via UnitOfWork.
"""

import json
from typing import Optional, List

from sqlalchemy import select, delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.database.models import Task
from app.database.seq_utils import allocate_seq_with_retry
from app.core.utils import generate_id

logger = get_logger(__name__)


class TaskRepository:
    """Repository for Task table operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    # ── Create ────────────────────────────────────────────────────────

    async def create(
        self,
        session_id: str,
        subject: str,
        description: str = "",
        status: str = "pending",
        metadata_json: Optional[dict] = None,
    ) -> Task:
        """Create a new task. Returns the ORM object. Retries on seq conflict.

        Self-commits so the unique constraint is visible to concurrent
        connections.  For atomic bulk replacement use ``replace_all()``.
        """
        return await allocate_seq_with_retry(
            self._session,
            Task,
            Task.session_id,
            session_id,
            lambda seq: Task(
                id=generate_id(),
                session_id=session_id,
                subject=subject,
                description=description,
                status=status,
                metadata_json=json.dumps(metadata_json, ensure_ascii=False) if metadata_json else None,
                seq=seq,
            ),
        )

    async def replace_all(
        self,
        session_id: str,
        items: List[dict],
    ) -> List[Task]:
        """Atomically replace all tasks for a session.

        Deletes existing tasks and creates new ones in a single commit.
        If anything fails, the whole operation is rolled back so old tasks
        remain intact.
        """
        # Delete without committing (unlike delete_by_session which self-commits)
        await self._session.execute(
            sql_delete(Task).where(Task.session_id == session_id)
        )
        tasks = []
        for i, item in enumerate(items):
            task = Task(
                id=generate_id(),
                session_id=session_id,
                subject=item.get("content", item.get("subject", "")),
                description=item.get("description", ""),
                status=item.get("status", "pending"),
                seq=i,
            )
            self._session.add(task)
            tasks.append(task)
        await self._session.commit()
        return tasks

    # ── Read ──────────────────────────────────────────────────────────

    async def resolve_by_prefix(
        self, session_id: str, short_id: str,
    ) -> tuple[Optional[Task], Optional[str]]:
        """Resolve a task by full or prefix ID (min 8 chars), scoped to a session.

        Returns (task, error_message). One of them is None.
        """
        # Exact match first (session-scoped)
        result = await self._session.execute(
            select(Task).where(
                Task.session_id == session_id,
                Task.id == short_id,
            )
        )
        task = result.scalar_one_or_none()
        if task:
            return task, None
        # Prefix match (requires >= 8 chars)
        if len(short_id) < 8:
            return None, f"ID '{short_id}' is too short, please provide at least 8 characters"
        result = await self._session.execute(
            select(Task).where(
                Task.session_id == session_id,
                Task.id.startswith(short_id),
            )
        )
        matches = list(result.scalars().all())
        if len(matches) == 0:
            return None, f"No task found with ID starting with '{short_id}'"
        if len(matches) > 1:
            return None, f"ID '{short_id}' matched {len(matches)} tasks, please provide more characters"
        return matches[0], None

    async def get(self, task_id: str) -> Optional[Task]:
        result = await self._session.execute(
            select(Task).where(Task.id == task_id)
        )
        return result.scalar_one_or_none()

    async def list_active(self, session_id: str) -> List[Task]:
        """List non-deleted tasks."""
        result = await self._session.execute(
            select(Task)
            .where(Task.session_id == session_id, Task.status != "deleted")
            .order_by(Task.seq)
        )
        return list(result.scalars().all())

    # ── Update ────────────────────────────────────────────────────────

    async def update(self, task_id: str, **fields) -> Optional[Task]:
        """Update task fields. Returns updated task or None."""
        task = await self.get(task_id)
        if not task:
            return None
        for key, value in fields.items():
            if hasattr(task, key):
                if key == "metadata_json" and isinstance(value, dict):
                    value = json.dumps(value, ensure_ascii=False)
                setattr(task, key, value)
        return task

    # ── Delete ────────────────────────────────────────────────────────

    async def delete_by_session(self, session_id: str) -> int:
        """Delete ALL tasks for a session. Returns count."""
        result = await self._session.execute(
            sql_delete(Task).where(Task.session_id == session_id)
        )
        await self._session.commit()
        return result.rowcount
