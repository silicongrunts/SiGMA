"""Session fork: cutoff resolution, agent filtering, and orchestration."""

from types import SimpleNamespace

import pytest

from app.core.exceptions import SessionNotFoundError, ValidationError
import app.services.ai_service as ai_service_module
from app.services.ai_service import (
    _collect_referenced_agent_ids,
    _resolve_fork_cutoff,
)
from tests.ai.conftest import (
    FakeSessionRepo,
    FakeSessionTempService,
    FakeTaskStateRepo,
    make_fake_uow,
)


def _msg(mid, seq, role, content="", tool_calls=None, created_at=None):
    return SimpleNamespace(
        id=mid, seq=seq, role=role, content=content,
        tool_calls=tool_calls, tool_call_id=None, reasoning_content=None,
        token_count=0, cached_tokens=0, input_tokens=0,
        is_boundary=False, created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_resolve_fork_cutoff_user_message_is_last_copied():
    messages = [
        _msg("m0", 0, "user", "hi"),
        _msg("m1", 1, "assistant", "hello"),
        _msg("m2", 2, "user", "again"),
        _msg("m3", 3, "assistant", "sure"),
    ]
    assert _resolve_fork_cutoff(messages, "m2") == 2


def test_resolve_fork_cutoff_assistant_turn_extends_to_turn_end():
    messages = [
        _msg("m0", 0, "user", "hi"),
        _msg("m1", 1, "assistant", "", tool_calls='[{"id": "c1"}]'),
        _msg("m2", 2, "tool", "result"),
        _msg("m3", 3, "assistant", "final answer"),
        _msg("m4", 4, "user", "next turn"),
    ]
    assert _resolve_fork_cutoff(messages, "m1") == 3


def test_resolve_fork_cutoff_rejects_unknown_and_non_conversation_rows():
    with pytest.raises(ValidationError):
        _resolve_fork_cutoff([_msg("m0", 0, "user")], "missing")
    with pytest.raises(ValidationError):
        _resolve_fork_cutoff(
            [_msg("m0", 0, "user"), _msg("m1", 1, "system", "summary")], "m1",
        )


def test_collect_referenced_agent_ids_requires_prefix_reference():
    prefix = [
        _msg("m0", 0, "user", "hi"),
        _msg("m1", 1, "assistant", "", tool_calls='{"resume_id": "a1"}'),
        _msg("m2", 2, "tool", "<resume_id>a2</resume_id>\ndone"),
        _msg("m3", 3, "user", "later"),  # fork point here
        _msg("m4", 4, "tool", "<resume_id>a3</resume_id>"),  # after fork point
    ]
    assert _collect_referenced_agent_ids(prefix[:3], ["a1", "a2", "a3"]) == ["a1", "a2"]


# ---------------------------------------------------------------------------
# fork_session orchestration (fake UnitOfWork / repos / temp service)
# ---------------------------------------------------------------------------

# Stays local: the fork orchestration contract is stage_copy_messages with
# rewrite pairs — a copy-recording shape no other test needs.
class _FakeMessageRepo:
    def __init__(self, messages_by_session):
        self.messages_by_session = messages_by_session
        self.copies = []

    async def get_messages(self, session_id):
        return list(self.messages_by_session.get(session_id, []))

    async def stage_copy_messages(self, messages, dst_session_id, rewrite_pairs):
        self.copies.append((dst_session_id, [m.id for m in messages], rewrite_pairs))


class _AgentResumesDuringFork(_FakeMessageRepo):
    def __init__(self, messages_by_session):
        super().__init__(messages_by_session)
        self._source_reads = 0

    async def get_messages(self, session_id):
        messages = await super().get_messages(session_id)
        if session_id == "s1":
            self._source_reads += 1
            if self._source_reads == 2:
                self.messages_by_session["a1"].append(
                    _msg("am1", 1, "assistant", "resumed after fork point")
                )
        return messages


def _build_source():
    """Source session with two agents: a1 referenced before, a2 after the m2 fork point."""
    source = SimpleNamespace(
        id="s1", project_id="p1", title="Original", session_kind="chat",
        agent_type=None, parent_session_id=None, parent_tool_call_id=None,
    )
    agent1 = SimpleNamespace(
        id="a1", project_id="p1", title="Agent: general", session_kind="agent",
        agent_type="general", parent_session_id="s1", parent_tool_call_id="c1",
    )
    agent2 = SimpleNamespace(
        id="a2", project_id="p1", title="Agent: general", session_kind="agent",
        agent_type="general", parent_session_id="s1", parent_tool_call_id="c2",
    )
    messages = [
        _msg("m0", 0, "user", "hi"),
        _msg("m1", 1, "assistant", "", tool_calls='{"resume_id": "a1"}'),
        _msg("m2", 2, "tool", "<resume_id>a1</resume_id>\ndone"),
        _msg("m3", 3, "user", "again"),
        _msg("m4", 4, "tool", "<resume_id>a2</resume_id>"),  # after fork point
    ]
    agent_messages = [_msg("am0", 0, "user", "agent prompt")]
    return source, agent1, agent2, messages, agent_messages


def _install(
    monkeypatch, source, agents, messages, agent_messages,
    message_repo=None, fail_atomic=False,
):
    session_repo = FakeSessionRepo(
        {source.id: source, **{a.id: a for a in agents}},
        descendants={
            source.id: [source.id] + [a.id for a in agents],
            **{a.id: [a.id] for a in agents},
        },
    )
    if message_repo is None:
        message_repo = _FakeMessageRepo({
            source.id: messages,
            **{a.id: agent_messages for a in agents},
        })
    temp = FakeSessionTempService()
    uow_cls = make_fake_uow(
        sessions=session_repo, messages=message_repo,
        task_state=FakeTaskStateRepo(),  # every fork test runs on an idle session
        fail_atomic=fail_atomic,
    )
    monkeypatch.setattr(ai_service_module, "UnitOfWork", uow_cls)
    monkeypatch.setattr(ai_service_module, "session_temp_service", temp)
    return session_repo, message_repo, temp


def _parent_stage(session_repo):
    return next(
        s for s in session_repo.staged if s.get("session_kind", "chat") == "chat"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fork_session_copies_prefix_and_referenced_agents(monkeypatch):
    source, agent1, agent2, messages, agent_messages = _build_source()
    session_repo, message_repo, temp = _install(
        monkeypatch, source, [agent1, agent2], messages, agent_messages,
    )

    result = await ai_service_module.ai_service.fork_session(
        "p1", "s1", "m1", title="My fork",
    )

    parent = _parent_stage(session_repo)
    new_parent_id = parent["session_id"]
    assert parent["title"] == "My fork"
    assert result["id"] == new_parent_id
    assert result["title"] == "My fork"

    # Only a1 (referenced inside the prefix) is copied; a2 is left behind.
    agent_stages = [s for s in session_repo.staged if s.get("session_kind") == "agent"]
    assert len(agent_stages) == 1
    new_agent_id = agent_stages[0]["session_id"]
    assert agent_stages[0]["parent_session_id"] == new_parent_id
    assert agent_stages[0]["agent_type"] == "general"

    # Message copies: parent prefix ends at m2; agent session copied fully.
    parent_copy = next(c for c in message_repo.copies if c[0] == new_parent_id)
    assert parent_copy[1] == ["m0", "m1", "m2"]
    agent_copy = next(c for c in message_repo.copies if c[0] == new_agent_id)
    assert agent_copy[1] == ["am0"]

    # Rewrites remap the source and agent ids; files copied both ways.
    pairs = parent_copy[2]
    assert ("s1", new_parent_id) in pairs
    assert ("a1", new_agent_id) in pairs
    assert ("a2", new_agent_id) not in pairs
    assert set(temp.copied) == {("s1", new_parent_id), ("a1", new_agent_id)}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fork_agent_history_stops_at_parent_fork_boundary(monkeypatch):
    source, agent1, agent2, messages, _ = _build_source()
    messages[1].created_at = 2
    messages[2].created_at = 4
    agent_messages = [
        _msg("am0", 0, "user", "agent prompt", created_at=3),
        _msg("am1", 1, "assistant", "resume after parent fork", created_at=5),
    ]
    session_repo, message_repo, _ = _install(
        monkeypatch, source, [agent1, agent2], messages, agent_messages,
    )

    await ai_service_module.ai_service.fork_session("p1", "s1", "m1")

    new_agent_id = next(
        s["session_id"] for s in session_repo.staged if s.get("session_kind") == "agent"
    )
    agent_copy = next(c for c in message_repo.copies if c[0] == new_agent_id)
    assert agent_copy[1] == ["am0"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fork_rejects_agent_resume_after_snapshot(monkeypatch):
    source, agent1, agent2, messages, agent_messages = _build_source()
    session_repo, _, _ = _install(
        monkeypatch, source, [agent1, agent2], messages, agent_messages,
        message_repo=_AgentResumesDuringFork({
            source.id: messages, agent1.id: agent_messages, agent2.id: agent_messages,
        }),
    )

    with pytest.raises(ValidationError, match="agent changed"):
        await ai_service_module.ai_service.fork_session("p1", "s1", "m1")

    assert not [row for row in session_repo.staged if row.get("session_kind") == "agent"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fork_session_falls_back_to_source_title(monkeypatch):
    source, agent1, agent2, messages, agent_messages = _build_source()
    session_repo, _, _ = _install(
        monkeypatch, source, [agent1], messages, agent_messages,
    )
    result = await ai_service_module.ai_service.fork_session("p1", "s1", "m0")
    assert result["title"] == "Original"
    assert _parent_stage(session_repo)["title"] == "Original"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fork_session_cleans_up_after_atomic_failure(monkeypatch):
    source, agent1, agent2, messages, agent_messages = _build_source()
    session_repo, _, temp = _install(
        monkeypatch, source, [agent1], messages, agent_messages,
        fail_atomic=True,
    )

    with pytest.raises(RuntimeError):
        await ai_service_module.ai_service.fork_session("p1", "s1", "m1")

    parent_id = _parent_stage(session_repo)["session_id"]
    new_ids = {s["session_id"] for s in session_repo.staged}
    assert session_repo.deleted == [parent_id]  # repo delete cascades to children
    assert set(temp.deleted) == new_ids


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fork_session_cleans_up_after_temp_copy_failure(monkeypatch):
    source, agent1, agent2, messages, agent_messages = _build_source()
    session_repo, _, temp = _install(
        monkeypatch, source, [agent1], messages, agent_messages,
    )
    temp.fail_copy_after = 1  # parent dir copies, the agent dir copy fails

    with pytest.raises(RuntimeError):
        await ai_service_module.ai_service.fork_session("p1", "s1", "m1")

    assert session_repo.staged == []  # rows were never staged
    assert len(temp.copied) == 1      # second copy failed
    # Every planned new id is cleaned, including the half-copied destination.
    assert len(temp.deleted) == 2
    assert temp.copied[0][1] in temp.deleted


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fork_session_rejects_missing_session_and_agent_kind(monkeypatch):
    source, agent1, agent2, messages, agent_messages = _build_source()
    _install(monkeypatch, source, [agent1], messages, agent_messages)

    with pytest.raises(SessionNotFoundError):
        await ai_service_module.ai_service.fork_session("p1", "missing", "m0")

    agent_source = SimpleNamespace(
        id="a1", project_id="p1", title="Agent", session_kind="agent",
        agent_type="general", parent_session_id=None, parent_tool_call_id=None,
    )
    _install(monkeypatch, agent_source, [], [_msg("am0", 0, "user")], [])
    with pytest.raises(ValidationError):
        await ai_service_module.ai_service.fork_session("p1", "a1", "am0")
