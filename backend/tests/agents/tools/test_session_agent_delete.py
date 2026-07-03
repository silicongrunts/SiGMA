from sqlalchemy import func, select

import pytest

from app.database.models import Message, Session, TaskState
from app.database.repos.session_repo import SessionRepository
from app.database.repos.message_repo import MessageRepository
from app.database.repos.task_state_repo import TaskStateRepository


@pytest.mark.asyncio
async def test_delete_chat_session_deletes_descendant_agent_sessions(db_session_factory):
    async with db_session_factory() as db:
        sessions = SessionRepository(db)
        messages = MessageRepository(db)
        task_states = TaskStateRepository(db)

        chat = await sessions.create("project-a", title="Chat")
        general = await sessions.create_agent_session(
            "project-a", "general", parent_session_id=chat.id,
        )
        plan = await sessions.create_agent_session(
            "project-a", "plan", parent_session_id=general.id,
        )

        await messages.create(chat.id, "user", "hello")
        await messages.create(general.id, "assistant", "agent work")
        await messages.create(plan.id, "assistant", "plan work")
        await task_states.set_queued("task-chat", session_id=chat.id)
        await task_states.set_queued("task-general", session_id=general.id)
        await task_states.set_queued("task-plan", session_id=plan.id)

        assert await sessions.delete(chat.id) is True

        session_count = await db.scalar(select(func.count()).select_from(Session))
        message_count = await db.scalar(select(func.count()).select_from(Message))
        task_state_count = await db.scalar(select(func.count()).select_from(TaskState))

        assert session_count == 0
        assert message_count == 0
        assert task_state_count == 0
