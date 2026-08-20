"""Staged session/message copy primitives used by session fork."""

import pytest

from app.database.repos.message_repo import MessageRepository
from app.database.repos.session_repo import SessionRepository


@pytest.mark.database
@pytest.mark.asyncio
async def test_stage_create_persists_only_on_commit(db_session_factory):
    async with db_session_factory() as db:
        sessions = SessionRepository(db)

        staged = await sessions.stage_create(
            "project-1", session_id="fork-1", title="Forked",
        )
        assert staged.id == "fork-1"
        await db.rollback()  # staged rows vanish without a commit
        assert await sessions.get_by_id("fork-1") is None

        await sessions.stage_create(
            "project-1", session_id="fork-1", title="Forked",
        )
        await db.commit()
        found = await sessions.get_by_id("fork-1")
        assert found is not None
        assert found.title == "Forked"
        assert found.session_kind == "chat"


@pytest.mark.database
@pytest.mark.asyncio
async def test_stage_create_agent_fields_round_trip(db_session_factory):
    async with db_session_factory() as db:
        sessions = SessionRepository(db)

        await sessions.stage_create(
            "project-1",
            session_id="agent-2",
            title="Agent: general",
            session_kind="agent",
            agent_type="general",
            parent_session_id="parent-1",
            parent_tool_call_id="call-9",
        )
        await db.commit()

        agent = await sessions.get_agent_session("agent-2", agent_type="general")
        assert agent is not None
        assert agent.parent_session_id == "parent-1"
        assert agent.parent_tool_call_id == "call-9"


@pytest.mark.database
@pytest.mark.asyncio
async def test_stage_copy_messages_preserves_fields_and_applies_rewrites(db_session_factory):
    async with db_session_factory() as db:
        sessions = SessionRepository(db)
        messages = MessageRepository(db)

        src = await sessions.create("project-1", title="Source")
        dst = await sessions.create("project-1", title="Fork")
        src_id, dst_id = src.id, dst.id  # rows expire on rollback below
        await messages.create(
            src_id, role="user",
            content="see .SiGMA/sessions/%s/chat_attachments/a.png" % src_id,
        )
        await messages.create(
            src_id, role="assistant", content="", token_count=5, cached_tokens=2,
            tool_calls='[{"id": "c1", "function": {"name": "agent",'
                       ' "arguments": "{\\"resume_id\\": \\"old-agent\\"}"}}]',
            reasoning_content="resume old-agent, scratch .SiGMA/sessions/%s/" % src_id,
        )
        await messages.create(
            src_id, role="tool", tool_call_id="c1",
            content="<resume_id>old-agent</resume_id>\ndone", is_boundary=False,
        )

        pairs = [
            (".SiGMA/sessions/%s/" % src_id, ".SiGMA/sessions/dst-new/"),
            ("old-agent", "new-agent"),
        ]
        rows = await messages.get_messages(src_id)
        source_ids = [r.id for r in rows]
        await messages.stage_copy_messages(rows, dst_id, pairs)
        await db.rollback()
        assert await messages.get_messages(dst_id) == []  # staged, not committed

        rows = await messages.get_messages(src_id)
        await messages.stage_copy_messages(rows, dst_id, pairs)
        await db.commit()
        copies = await messages.get_messages(dst_id)
        assert [c.seq for c in copies] == [0, 1, 2]
        assert [c.id for c in copies] != source_ids  # fresh ids
        assert copies[0].content.endswith("chat_attachments/a.png")
        assert ".SiGMA/sessions/dst-new/" in copies[0].content
        assert "new-agent" in copies[1].tool_calls
        assert "<resume_id>new-agent</resume_id>" in copies[2].content
        assert copies[1].token_count == 5 and copies[1].cached_tokens == 2
        # Reasoning is replayed to the LLM, so ids inside it are remapped too.
        assert copies[1].reasoning_content == (
            "resume new-agent, scratch .SiGMA/sessions/dst-new/"
        )
        assert copies[2].tool_call_id == "c1"


@pytest.mark.database
@pytest.mark.asyncio
async def test_collect_descendant_session_ids_orders_children_first(db_session_factory):
    async with db_session_factory() as db:
        sessions = SessionRepository(db)

        root = await sessions.create("project-1", title="Root")
        child = await sessions.create_agent_session(
            "project-1", "general", parent_session_id=root.id,
        )
        grandchild = await sessions.create_agent_session(
            "project-1", "general", parent_session_id=child.id,
        )

        ids = await sessions.collect_descendant_session_ids(root.id)

        assert set(ids) == {root.id, child.id, grandchild.id}
        assert ids.index(grandchild.id) < ids.index(child.id) < ids.index(root.id)
