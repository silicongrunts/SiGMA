"""
Session Repository — CRUD operations for Session model.

Only this file (and other files in database/) may import Session directly.
"""

from typing import Optional, List

from sqlalchemy import select, update, delete as sql_delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Session, Message, Task, TaskState
from app.core.utils import utcnow


class SessionRepository:
    """Repository for Session table operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, project_id: str, title: str = "",
                     session_kind: str = "chat") -> Session:
        """Create a new session. Auto-generates 'Untitled-n' title if none provided.

        ``project_id`` is written to the row to satisfy the NOT NULL column;
        it is not used for filtering since this DB is already project-scoped.
        """
        if not title:
            result = await self._session.execute(
                select(func.count()).select_from(Session)
                .where(Session.session_kind == session_kind)
            )
            count = result.scalar_one()
            title = f"Untitled-{count + 1}"
        db_session = Session(project_id=project_id, title=title,
                             session_kind=session_kind)
        self._session.add(db_session)
        await self._session.commit()
        await self._session.refresh(db_session)
        return db_session

    async def get_by_id(self, session_id: str) -> Optional[Session]:
        result = await self._session.execute(
            select(Session).where(Session.id == session_id)
        )
        return result.scalar_one_or_none()

    async def list_all(
        self, include_archived: bool = False,
        session_kind: str = "chat",
    ) -> List[Session]:
        """List sessions in this project DB, ordered by most recently updated.

        No project filter is needed: this DB file is already project-scoped
        (one SQLite file per project under ``userdata/<id>/.SiGMA/``).

        By default only returns sessions matching session_kind (defaults to "chat",
        hiding agent sessions from the user-facing session list).
        """
        query = select(Session)
        if session_kind:
            query = query.where(Session.session_kind == session_kind)
        if not include_archived:
            query = query.where(Session.is_archived == False)  # noqa: E712
        query = query.order_by(Session.updated_at.desc())
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def update(self, session_id: str, **fields) -> None:
        """Update session fields (title, is_archived)."""
        values = {k: v for k, v in fields.items() if hasattr(Session, k)}
        if values:
            values["updated_at"] = utcnow()
            await self._session.execute(
                update(Session).where(Session.id == session_id).values(**values)
            )
            await self._session.commit()

    async def delete(self, session_id: str) -> bool:
        """Delete a session and its hidden descendant agent sessions."""
        result = await self._session.execute(
            select(Session).where(Session.id == session_id)
        )
        root = result.scalar_one_or_none()
        if root is None:
            return False

        session_ids = await self._collect_descendant_session_ids(session_id)

        # Delete children first.  This is explicit instead of relying on ORM/DB
        # cascades so async bulk deletes behave consistently across SQLite and
        # production databases, and so TaskState rows without FKs are cleaned too.
        ids = list(session_ids)
        await self._session.execute(
            sql_delete(TaskState).where(TaskState.session_id.in_(ids))
        )
        await self._session.execute(
            sql_delete(Task).where(Task.session_id.in_(ids))
        )
        await self._session.execute(
            sql_delete(Message).where(Message.session_id.in_(ids))
        )
        await self._session.execute(
            sql_delete(Session).where(Session.id.in_(ids))
        )

        await self._session.commit()
        return True

    async def _collect_descendant_session_ids(self, session_id: str) -> list[str]:
        """Return session_id plus all descendant agent sessions, children first."""
        ordered = [session_id]
        frontier = [session_id]
        while frontier:
            result = await self._session.execute(
                select(Session.id).where(Session.parent_session_id.in_(frontier))
            )
            children = [
                sid for sid in result.scalars().all()
                if sid not in ordered
            ]
            if not children:
                break
            ordered.extend(children)
            frontier = children
        return list(reversed(ordered))

    async def stage_touch(self, session_id: str) -> None:
        """Stage updated_at refresh without committing."""
        await self._session.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(updated_at=utcnow())
        )

    # ── Agent session helpers ──

    async def create_agent_session(
        self,
        project_id: str,
        agent_type: str,
        parent_session_id: str = "",
        parent_tool_call_id: str = "",
    ) -> Session:
        """Create a hidden agent session. Not visible in session list UI."""
        title = f"Agent: {agent_type}"
        db_session = Session(
            project_id=project_id,
            title=title,
            session_kind="agent",
            agent_type=agent_type,
            parent_session_id=parent_session_id or None,
            parent_tool_call_id=parent_tool_call_id or None,
        )
        self._session.add(db_session)
        await self._session.commit()
        await self._session.refresh(db_session)
        return db_session

    async def get_agent_session(self, session_id: str,
                                agent_type: str = "general") -> Optional[Session]:
        """Get an agent session by ID, validating kind and type.

        Returns None if the session doesn't exist, isn't an agent session,
        or doesn't match the expected agent_type.
        """
        result = await self._session.execute(
            select(Session).where(
                Session.id == session_id,
                Session.session_kind == "agent",
                Session.agent_type == agent_type,
            )
        )
        return result.scalar_one_or_none()
