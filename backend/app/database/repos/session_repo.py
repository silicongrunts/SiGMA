"""
Session Repository — CRUD operations for Session model.

Only this file (and other files in database/) may import Session directly.
"""

from typing import Optional, List

from sqlalchemy import select, update, delete as sql_delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Session, Message, Task, TaskState
from app.database.repos.task_state_repo import TaskStateRepository
from app.core.utils import generate_id, utcnow
from app.core.exceptions import SessionNotFoundError, SessionDeletingError


SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_DELETING = "deleting"


class SessionRepository:
    """Repository for Session table operations."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, title: str = "",
                     session_kind: str = "chat") -> Session:
        """Create a new session. Auto-generates 'Untitled-n' title if none provided."""
        if not title:
            result = await self._session.execute(
                select(func.count()).select_from(Session)
                .where(Session.session_kind == session_kind)
            )
            count = result.scalar_one()
            title = f"Untitled-{count + 1}"
        db_session = Session(title=title, session_kind=session_kind)
        self._session.add(db_session)
        await self._session.commit()
        await self._session.refresh(db_session)
        return db_session

    async def stage_create(
        self,
        *,
        session_id: str = "",
        title: str = "",
        session_kind: str = "chat",
        agent_type: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        parent_tool_call_id: Optional[str] = None,
    ) -> Session:
        """Stage a new session row without committing.

        ``session_id`` may be supplied explicitly so callers can prepare
        dependent state (files, id rewrites) before the atomic commit; the
        ORM default id generator fills it otherwise. Unlike ``create``, no
        Untitled fallback title is generated — the caller owns the title.
        """
        db_session = Session(
            id=session_id or generate_id(),
            title=title,
            session_kind=session_kind,
            agent_type=agent_type,
            parent_session_id=parent_session_id,
            parent_tool_call_id=parent_tool_call_id,
        )
        self._session.add(db_session)
        return db_session

    async def get_by_id(self, session_id: str) -> Optional[Session]:
        result = await self._session.execute(
            select(Session).where(Session.id == session_id)
        )
        return result.scalar_one_or_none()

    async def assert_writable(self, session_id: str) -> Session:
        """Return a session that accepts new persistent work."""
        db_session = await self.get_by_id(session_id)
        if db_session is None:
            raise SessionNotFoundError(session_id)
        if db_session.lifecycle_status == SESSION_STATUS_DELETING:
            raise SessionDeletingError(session_id)
        return db_session

    async def begin_delete(self, session_id: str, *, commit: bool = True) -> tuple[list[str], list[str]]:
        """Install the deleting barrier and cancel descendant task rows."""
        root = await self.get_by_id(session_id)
        if root is None:
            return [], []
        session_ids = await self.collect_descendant_session_ids(session_id)
        task_state = TaskStateRepository(self._session)
        task_ids = await task_state.cancel_for_sessions(
            session_ids, commit=False,
        )
        await self._session.execute(
            update(Session)
            .where(
                Session.id.in_(session_ids),
                Session.lifecycle_status == SESSION_STATUS_ACTIVE,
            )
            .values(lifecycle_status=SESSION_STATUS_DELETING)
        )
        if commit:
            await self._session.commit()
        return session_ids, task_ids

    async def delete_marked(self, session_id: str) -> bool:
        """Delete a session tree only after its durable barrier is present."""
        root = await self.get_by_id(session_id)
        if root is None:
            return False
        if root.lifecycle_status != SESSION_STATUS_DELETING:
            raise SessionDeletingError(session_id)
        return await self.delete(session_id)

    async def list_deleting(self) -> list[str]:
        """Return root sessions carrying a deletion barrier."""
        result = await self._session.execute(
            select(Session.id, Session.parent_session_id).where(
                Session.lifecycle_status == SESSION_STATUS_DELETING,
            )
        )
        rows = list(result)
        deleting = {row.id for row in rows}
        return [
            row.id for row in rows
            if row.parent_session_id not in deleting
        ]

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

        session_ids = await self.collect_descendant_session_ids(session_id)

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

    async def collect_descendant_session_ids(self, session_id: str) -> list[str]:
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
        agent_type: str,
        parent_session_id: str = "",
        parent_tool_call_id: str = "",
    ) -> Session:
        """Create a hidden agent session. Not visible in session list UI."""
        if parent_session_id:
            claimed = await self._session.execute(
                update(Session)
                .where(
                    Session.id == parent_session_id,
                    Session.lifecycle_status == SESSION_STATUS_ACTIVE,
                )
                .values(updated_at=Session.updated_at)
            )
            if claimed.rowcount != 1:
                await self.assert_writable(parent_session_id)
        title = f"Agent: {agent_type}"
        db_session = Session(
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
