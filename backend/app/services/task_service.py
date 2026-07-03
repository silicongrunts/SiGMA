"""
Task Service — business logic for task CRUD.

Wraps TaskRepository via UnitOfWork so that agent tools and route handlers
do not import the database layer directly.

Also provides ``task_to_dict()`` — a pure conversion function shared by
tools and services (ai_service, query_loop).
"""

import json
from typing import Optional, List

from app.core.logging import get_logger
from app.database.unit_of_work import UnitOfWork

logger = get_logger(__name__)


def task_to_dict(task) -> dict:
    """Convert an ORM Task object to a plain dict.

    Shared by agent tools (task_tools) and services (ai_service, query_loop)
    so that none of them needs to duplicate this logic or create a reverse
    dependency from services → agents/tools.
    """
    metadata_json = None
    if task.metadata_json:
        try:
            metadata_json = json.loads(task.metadata_json)
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "id": task.id,
        "subject": task.subject,
        "description": task.description,
        "status": task.status,
        "metadata": metadata_json,
    }


class TaskService:
    """Service-layer CRUD for tasks, delegating DB work to repositories."""

    async def create_task(
        self,
        project_id: str,
        session_id: str,
        subject: str,
        description: str = "",
        metadata_json: Optional[dict] = None,
    ):
        """Create a new task. Returns the ORM Task object.

        The underlying repo self-commits to enforce the unique seq constraint.
        """
        async with UnitOfWork(project_id) as uow:
            return await uow.tasks.create(
                session_id=session_id,
                subject=subject,
                description=description,
                metadata_json=metadata_json,
            )

    async def resolve_task(
        self, project_id: str, session_id: str, short_id: str,
    ) -> tuple:
        """Resolve a task by full or prefix ID, scoped to a session.

        Returns (task, error_message). One of them is None.
        """
        async with UnitOfWork(project_id) as uow:
            return await uow.tasks.resolve_by_prefix(session_id, short_id)

    async def get_task(self, project_id: str, task_id: str):
        """Get a task by exact ID. Returns ORM Task or None."""
        async with UnitOfWork(project_id) as uow:
            return await uow.tasks.get(task_id)

    async def update_task(self, project_id: str, task_id: str, **fields):
        """Update task fields. Returns the updated ORM Task (or None).

        ``TaskRepository.update`` does NOT self-commit, so ``uow.commit()``
        is required here. Pure field update — no side effects. Use
        ``cleanup_completed_tasks`` separately to soft-delete completed tasks.
        """
        async with UnitOfWork(project_id) as uow:
            task = await uow.tasks.update(task_id, **fields)
            await uow.commit()
            if task:
                # Re-read to get fresh state after commit
                task = await uow.tasks.get(task_id)
            return task

    async def cleanup_completed_tasks(self, project_id: str, session_id: str) -> int:
        """Soft-delete all completed tasks for a session when no active work remains.

        Returns the number of tasks cleared. No-op if any task is still
        pending or in-progress.
        """
        cleaned_count = 0
        async with UnitOfWork(project_id) as uow:
            active = await uow.tasks.list_active(session_id)
            if active and all(t.status == "completed" for t in active):
                for t in active:
                    await uow.tasks.update(t.id, status="deleted")
                await uow.commit()
                cleaned_count = len(active)
                logger.info(
                    "Auto-cleaned %d completed task(s) for session %s",
                    cleaned_count, session_id,
                )
        return cleaned_count

    async def list_active_tasks(
        self, project_id: str, session_id: str
    ) -> list:
        """List non-deleted tasks for a session. Returns list of ORM Task."""
        async with UnitOfWork(project_id) as uow:
            return await uow.tasks.list_active(session_id)

    async def replace_tasks(
        self, project_id: str, session_id: str, items: List[dict]
    ) -> list:
        """Atomically replace all tasks for a session.

        Uses ``replace_all()`` which deletes old tasks and inserts new ones
        in a single commit.
        """
        async with UnitOfWork(project_id) as uow:
            return await uow.tasks.replace_all(session_id, items)


# Singleton
task_service = TaskService()
