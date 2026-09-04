"""Chat pipeline core contracts that are not checkpoint matrices.

Complements the split theme files (permission pause/resume, submit claim,
task recovery, annotation arbitration): provider-failure persistence, history
rendering of interrupted/parked turns, session-history operations guarded by
task state (clear/delete/fork/edit-submit/skill load), terminal frame
scoping, and turn token stats across a pause/resume split.
"""

import asyncio
import json
from importlib import import_module

import pytest

import_module("app.agents.tools")
from app.core.exceptions import TaskActiveError, ValidationError
from app.core.message_format import shape_messages_for_ui
from app.core.utils import generate_id
from app.database.unit_of_work import UnitOfWork
from app.services import task_runtime
from app.services.ai_service import ai_service
from app.services.chat_executor import stream_chat_for_task
from app.services.llm_loop_runner import LLMLoopRunner
from app.services.query_loop import QueryLoop
from tests.ai.matrix_harness import (  # noqa: F401 (autouse fixture below)
    collect,
    load_state,
    load_state_row,
    make_turn,
    owned_response,
    park_permission_checkpoint,
    script_agent_tool_call,
    script_llm,
    script_main_tool_call,
    script_subagent_bash,
    start_running_task,
    use_fixture_project_root,
)


@pytest.mark.asyncio
async def test_llm_stream_failure_persists_error_before_streaming(project, monkeypatch):
    """A provider failure must persist the error bubble before the terminal
    event — a refresh must still show the failure."""
    script_llm(monkeypatch, [("raise", ConnectionError("provider dropped"))])
    session_id, task_id = await make_turn(project)

    events = await collect(QueryLoop(project, session_id, task_id=task_id))

    assert [e["type"] for e in events] == ["context_stats", "error"]
    assert "provider dropped" in events[-1]["data"]["error"]
    assert events[-1]["data"]["content"]

    _, _, messages = await load_state(project, session_id, task_id)
    assert messages[-1].role == "assistant"
    assert "provider dropped" in messages[-1].content


@pytest.mark.asyncio
async def test_dangling_tool_call_renders_interrupted_in_history(project):
    """A tool call whose result row never arrived (worker died mid-call)
    must render as interrupted after a refresh — never as a done step.

    Kept deliberately: tests/core/test_message_format.py pins the same
    shaping rule with in-memory rows; this is the end-to-end smoke over real
    DB rows through the repository layer."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
        await uow.messages.create(session_id=session.id, role="user", content="go")
        await uow.messages.create(
            session_id=session.id, role="assistant", content="",
            tool_calls=json.dumps([{
                "id": "call_dead", "type": "function",
                "function": {"name": "bash", "arguments": '{"command": "sleep 100"}'},
            }]),
        )
        await uow.messages.create(
            session_id=session.id, role="assistant", content="",
            tool_calls=json.dumps([{
                "id": "call_ok", "type": "function",
                "function": {"name": "read", "arguments": '{"file_path": "a.md"}'},
            }]),
        )
        await uow.messages.create(
            session_id=session.id, role="tool",
            content="file body", tool_call_id="call_ok",
        )
        messages = await uow.messages.get_messages(session.id)

    entries = shape_messages_for_ui(messages, boundary_seq=None)

    sigma = next(e for e in entries if e["role"] == "SiGMA")
    steps = {s["tool"]: s["status"] for s in sigma["process"] if s["type"] == "tool"}
    assert steps["bash"] == "interrupted"
    assert steps["read"] == "done"


@pytest.mark.asyncio
async def test_parked_history_renders_awaiting_input_then_done(project, monkeypatch):
    """A turn parked on a permission checkpoint persists an unpaired tool
    call. History shaped while parked must show it as awaiting_input — never
    interrupted — and carry its tool_call_id so a resumed stream can
    re-attach after a refresh. Once the approval completes the call, the
    same step renders done."""
    script_llm(monkeypatch, [
        script_agent_tool_call(),
        script_subagent_bash(),
        ("text", "subagent done"),
        ("text", "main done"),
    ])
    session_id, _ = await make_turn(project)

    task1 = await start_running_task(project, session_id)
    await collect(QueryLoop(project, session_id, task_id=task1))

    page = await ai_service.get_history(project, session_id=session_id)
    sigma = next(e for e in page["messages"] if e["role"] == "SiGMA")
    agent_steps = [
        s for s in sigma.get("process", [])
        if s.get("type") == "tool" and s["tool"] == "agent"
    ]
    assert agent_steps[-1]["status"] == "awaiting_input"
    assert agent_steps[-1]["tool_call_id"] == "call_agent"

    task2 = await start_running_task(project, session_id)
    await collect(QueryLoop(
        project, session_id, task_id=task2,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    page = await ai_service.get_history(project, session_id=session_id)
    sigma = next(e for e in page["messages"] if e["role"] == "SiGMA")
    agent_steps = [
        s for s in sigma.get("process", [])
        if s.get("type") == "tool" and s["tool"] == "agent"
    ]
    assert agent_steps[-1]["status"] == "done"


# ---------------------------------------------------------------------------
# clear_history / delete_session coordination with task_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_history_rejected_while_task_active(project):
    """clear_history refuses while a runnable task holds the session: the
    running loop's tail-slice persistence would resurrect deleted rows."""
    session_id, _ = await make_turn(project)  # queued row

    with pytest.raises(TaskActiveError):
        await ai_service.clear_history(project, session_id=session_id)

    _, _, messages = await load_state(project, session_id, None)
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_clear_history_rejected_while_parked(project):
    """A parked checkpoint must never lose its boundary rows: clear_history
    refuses an awaiting_input session too."""
    session_id, task_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, task_id)

    with pytest.raises(TaskActiveError):
        await ai_service.clear_history(project, session_id=session_id)

    _, interaction, _ = await load_state(project, session_id, task_id)
    assert interaction is not None


@pytest.mark.asyncio
async def test_clear_history_succeeds_after_completion(project):
    session_id, task_id = await make_turn(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(task_id)

    result = await ai_service.clear_history(project, session_id=session_id)

    assert result["success"] is True
    _, _, messages = await load_state(project, session_id, task_id)
    assert messages == []


@pytest.mark.asyncio
async def test_delete_session_drains_runnable_task(project):
    """A queued session task row is removed as part of session deletion."""
    session_id, task_id = await make_turn(project)  # queued row

    await ai_service.delete_session(project, session_id)

    async with UnitOfWork(project) as uow:
        assert await uow.sessions.get_by_id(session_id) is None
        # The queued TaskState row must go too, not just the session row.
        assert await uow.task_state.get_by_id(task_id) is None


@pytest.mark.asyncio
async def test_delete_session_removes_parked_checkpoint(project):
    """Deleting a session removes its parked interaction checkpoint."""
    session_id, task_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, task_id)

    await ai_service.delete_session(project, session_id)

    session, interaction, messages = await load_state(project, session_id, task_id)
    assert session is None
    assert interaction is None
    assert messages == []


@pytest.mark.asyncio
async def test_delete_session_succeeds_when_idle(project):
    session_id, task_id = await make_turn(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(task_id)

    await ai_service.delete_session(project, session_id)

    async with UnitOfWork(project) as uow:
        assert await uow.sessions.get_by_id(session_id) is None


@pytest.mark.asyncio
async def test_fork_session_rejected_while_task_active(project):
    """A fork taken mid-stream would bake a truncated partial checkpoint
    into the new session — the active-task guard refuses it like
    delete_session does."""
    session_id, _ = await make_turn(project)  # queued row

    with pytest.raises(TaskActiveError):
        await ai_service.fork_session(project, session_id, "m1")

    async with UnitOfWork(project) as uow:
        assert await uow.sessions.list_all() != []


@pytest.mark.asyncio
async def test_fork_session_rejected_while_parked(project):
    session_id, task_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, task_id)

    with pytest.raises(TaskActiveError):
        await ai_service.fork_session(project, session_id, "m1")

    _, interaction, _ = await load_state(project, session_id, task_id)
    assert interaction is not None


@pytest.mark.asyncio
async def test_fork_session_succeeds_when_idle(project):
    session_id, task_id = await make_turn(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(task_id)
        messages = await uow.messages.get_messages(session_id)

    forked = await ai_service.fork_session(project, session_id, messages[0].id)

    assert forked["id"] != session_id
    async with UnitOfWork(project) as uow:
        forked_messages = await uow.messages.get_messages(forked["id"])
    assert [m.content for m in forked_messages] == ["do the work"]


# ---------------------------------------------------------------------------
# edit_and_submit_chat claim ordering
# ---------------------------------------------------------------------------


async def make_editable_session(project):
    """A session with two turns; returns (session_id, last_user_message)."""
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()
        await uow.messages.create(session_id=session.id, role="user", content="first")
        await uow.messages.create(session_id=session.id, role="assistant", content="reply")
        last = await uow.messages.create(session_id=session.id, role="user", content="third")
    return session.id, last


@pytest.mark.asyncio
async def test_edit_and_submit_loses_claim_race_without_mutating_messages(
    project, monkeypatch,
):
    """The claim precedes the message mutation: a concurrent submit that won
    the queued-row insert makes the edit 409 BEFORE its truncation, so the
    winning task's history is intact."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, last = await make_editable_session(project)
    holder = generate_id()
    async with UnitOfWork(project) as uow:
        await uow.task_state.set_queued(holder, session_id=session_id)

    with pytest.raises(TaskActiveError):
        await ai_service.edit_and_submit_chat(
            project_id=project, session_id=session_id,
            message_id=last.id, message="edited", context={},
        )

    _, _, messages = await load_state(project, session_id, holder)
    assert [m.content for m in messages] == ["first", "reply", "third"]
    assert launched == []


@pytest.mark.asyncio
async def test_edit_and_submit_rewrites_and_launches_when_idle(project, monkeypatch):
    launched = []
    sources = []

    def fake_launch(**kwargs):
        launched.append(kwargs)
        kwargs["source_factory"](asyncio.Event())

    monkeypatch.setattr(task_runtime, "launch", fake_launch)
    monkeypatch.setattr(
        "app.services.chat_executor.stream_chat_for_task",
        lambda **kwargs: sources.append(kwargs),
    )
    session_id, last = await make_editable_session(project)

    result = await ai_service.edit_and_submit_chat(
        project_id=project, session_id=session_id,
        message_id=last.id, message="edited", context={},
    )

    # Exactly one claim, one launch, and the rewrite truncated from the
    # edited message's seq.
    assert len(launched) == 1
    assert launched[0]["task_id"] == result["task_id"]
    assert len(sources) == 1
    _, _, messages = await load_state(project, session_id, result["task_id"])
    assert len(messages) == 3
    assert [m.content for m in messages[:2]] == ["first", "reply"]
    assert messages[-1].role == "user"
    assert "edited" in messages[-1].content
    row = await load_state_row(project, result["task_id"])
    assert row["status"] == "queued"


# ---------------------------------------------------------------------------
# load_skill_into_session tail ownership
# ---------------------------------------------------------------------------


def _fake_skill_service(monkeypatch):
    """Serve one enabled skill without touching the on-disk skill library."""
    import app.services.skill_service as skill_service_module

    monkeypatch.setattr(
        skill_service_module.skill_service, "get_all_skills",
        lambda: [{"id": "skill-1", "name": "Skill One", "enabled": True}],
    )
    monkeypatch.setattr(
        skill_service_module.skill_service, "get_skill_content",
        lambda skill_id: "skill body",
    )


@pytest.mark.asyncio
async def test_load_skill_rejected_while_task_active(project, monkeypatch):
    """The four skill rows land on the session message tail, which a running
    chat turn owns: loading while a task holds the session would shift the
    running loop's tail-slice accounting and silently skip its newest
    messages at the next save."""
    _fake_skill_service(monkeypatch)
    session_id, _ = await make_turn(project)  # queued row holds the session

    with pytest.raises(TaskActiveError):
        await ai_service.load_skill_into_session(project, session_id, "skill-1")

    _, _, messages = await load_state(project, session_id, None)
    assert [m.role for m in messages] == ["user"]


@pytest.mark.asyncio
async def test_load_skill_rejected_for_agent_session(project, monkeypatch):
    """An agent session's message tail belongs to its parent task — skill
    injection is a chat-session operation only."""
    _fake_skill_service(monkeypatch)
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create(session_kind="agent")

    with pytest.raises(ValidationError):
        await ai_service.load_skill_into_session(project, session.id, "skill-1")

    _, _, messages = await load_state(project, session.id, None)
    assert messages == []


@pytest.mark.asyncio
async def test_load_skill_appends_full_turn_when_idle(project, monkeypatch):
    _fake_skill_service(monkeypatch)
    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()

    result = await ai_service.load_skill_into_session(project, session.id, "skill-1")

    assert result["skill_id"] == "skill-1"
    _, _, messages = await load_state(project, session.id, None)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    tool_row = messages[2]
    assert tool_row.tool_call_id
    assert tool_row.content == "skill body"


# ---------------------------------------------------------------------------
# /chat/stream/{task_id} terminal fallback scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_frame_scoped_to_caller_project(project):
    """With the caller's project_id the finished-task lookup stays inside
    that project: the task's own project renders its terminal frame, a wrong
    project never finds the row."""
    session_id, task_id = await make_turn(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(task_id)

    frame = await ai_service._terminal_frame_without_session(task_id, project)
    assert "event: done" in frame

    frame = await ai_service._terminal_frame_without_session(task_id, "other-project")
    assert "event: error" in frame


# ---------------------------------------------------------------------------
# /annotations/stream/{task_id} terminal fallback scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotation_stream_scoped_to_caller_project(project):
    """The annotation resume route forwards the caller's project_id, so the
    finished-task lookup stays inside that project: the task's own project
    renders its terminal frame, a wrong project never finds the row."""
    from app.routes.annotations import resume_annotation_stream

    session_id, task_id = await make_turn(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(task_id)

    response = await resume_annotation_stream(task_id, cursor=None, project_id=project)
    frames = [frame async for frame in response.body_iterator]
    assert any("event: done" in frame for frame in frames)

    response = await resume_annotation_stream(
        task_id, cursor=None, project_id="other-project",
    )
    frames = [frame async for frame in response.body_iterator]
    assert any("event: error" in frame for frame in frames)


# ---------------------------------------------------------------------------
# Turn token stats across pause/resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_usage_stays_monotonic_across_pause_resume(project, monkeypatch):
    """A permission pause splits one user turn across two chat tasks; the
    reported turn stats must not reset at the split (the regression behind
    the shrinking output-token display). The resume's tracker seeds the
    parked segment's persisted spend as its baseline, so its first reading
    already dominates the pause segment's last one."""
    calls = {"n": 0}

    async def fake_stream_llm(ctx, messages, delta_queue):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("", "", [script_main_tool_call()[1][0]], {
                "prompt_tokens": 100, "completion_tokens": 30,
            })
        # Completion SHRINKS on the resume segment: per-task accounting
        # would drop the display from 30 to 5.
        return ("", "", [], {
            "prompt_tokens": 200, "completion_tokens": 5,
        })

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm))
    session_id, parked = await make_turn(project)

    async def collect_stream(**kwargs):
        return [event async for event in stream_chat_for_task(**kwargs)]

    pause_events = await collect_stream(
        project_id=project, context={}, session_id=session_id, task_id=parked,
    )
    pause_usage = [e["data"]["usage"] for e in pause_events if e["type"] == "turn_usage"]
    assert pause_usage[-1] == {"input": 100, "output": 30, "cached": 0}
    assert pause_events[-1]["type"] == "awaiting_input"

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect_stream(
        project_id=project, context={}, session_id=session_id, task_id=resume_task,
        interaction_response=await owned_response(
            project, session_id, {"approved": True},
        ),
    )
    resume_usage = [e["data"]["usage"] for e in resume_events if e["type"] == "turn_usage"]
    assert resume_usage, "the resume segment must report turn usage"
    for usage in resume_usage:
        assert usage["input"] >= 100
        assert usage["output"] >= 30
    # done carries base (100/30 — the parked assistant row) + this task's
    # accrual (200/5) as one whole-turn figure.
    assert resume_events[-1]["type"] == "done"
    assert resume_events[-1]["data"]["usage"] == {
        "input": 300, "output": 35, "cached": 0,
    }
