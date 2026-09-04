"""Permission pause/resume matrix for the chat pipeline.

Drives the REAL QueryLoop -> LLMLoopRunner -> permission executor -> agent
tool chain against a real per-project SQLite database; only the LLM is
scripted. Each test pins one permission/interactive checkpoint scenario's
full observable contract across pause, resume, and re-park: SSE events,
persisted messages, and task_state transitions — including target-drift and
cell-digest approval binding for direct, subagent, and fork sub-loops.
"""

import hashlib
import json
from importlib import import_module
from pathlib import Path

import pytest
from sqlalchemy import update

import_module("app.agents.tools")
from app.agents.tools.read_state import read_state_cache
from app.core.config import settings
from app.core.text_diff import CONTENT_SOFT_LIMIT
from app.database.unit_of_work import UnitOfWork
from app.services.file_service import file_service
from app.services.llm_loop_runner import LLMLoopRunner
from app.services.query_loop import QueryLoop
from tests.ai.matrix_harness import (  # noqa: F401 (autouse fixture below)
    collect,
    load_state,
    make_turn,
    owned_response,
    script_agent_tool_call,
    script_edit_tool_call,
    script_fork_agent_tool_call,
    script_fork_ask_user,
    script_main_tool_call,
    script_notebook_read_call,
    script_notebook_run_call,
    script_parallel_batch,
    script_plan_agent_tool_call,
    script_plan_submission,
    script_read_tool_call,
    script_subagent_bash,
    script_subagent_write,
    script_write_tool_call,
    start_running_task,
    use_fixture_project_root,
    script_llm,
)


@pytest.mark.asyncio
async def test_main_loop_permission_pause_parks_task(project, monkeypatch):
    """Main-loop bash permission gate → awaiting_input checkpoint, not failure."""
    script_llm(monkeypatch, [script_main_tool_call()])
    session_id, task_id = await make_turn(project)

    events = await collect(QueryLoop(project, session_id, task_id=task_id))

    types = [e["type"] for e in events]
    assert types[0] == "context_stats"
    # Parking yields exactly one awaiting_input frame and NO done: the task
    # runner synthesizes the terminal done when it sees the parked status.
    assert types[-1] == "awaiting_input"
    assert "done" not in types
    assert "error" not in types and "cancelled" not in types
    assert types.index("tool_start") < types.index("awaiting_input")
    status, interaction, messages = await load_state(project, session_id, task_id)
    assert status == "awaiting_input"
    assert interaction["interaction_data"]["interaction_type"] == "permission"
    assert interaction["interaction_data"]["task_id"] == task_id
    assert interaction["tool_name"] == "bash"
    assert "mkdir" in interaction["tool_args"]["command"]
    # The SSE dialog payload carries the same task id as the checkpoint.
    assert events[-1]["data"]["task_id"] == task_id
    roles = [(m.role, bool(m.tool_calls)) for m in messages]
    assert roles == [("user", False), ("assistant", True)]


@pytest.mark.asyncio
async def test_main_loop_permission_approve_resume_executes_tool(project, monkeypatch):
    script_llm(monkeypatch, [script_main_tool_call(), ("text", "dir created")])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    assert resume_events[-1]["type"] == "done"
    tool_end = next(e for e in resume_events if e["type"] == "tool_end")
    assert tool_end["data"]["tool_call_id"] == "call_main"
    assert (settings.get_project_path(project) / "made_by_main").is_dir()

    # The resume's claim consumes the parked row; finalizing the resume's own
    # row is the worker's job (this harness drives QueryLoop directly).
    parked_status, interaction, messages = await load_state(project, session_id, parked)
    assert parked_status == "completed"
    assert interaction is None
    tool_rows = [m for m in messages if m.role == "tool"]
    assert len(tool_rows) == 1


@pytest.mark.asyncio
async def test_main_loop_permission_deny_resume_injects_denial(project, monkeypatch):
    script_llm(monkeypatch, [script_main_tool_call(), ("text", "understood")])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": False, "reason": "not now"}),
    ))

    assert resume_events[-1]["type"] == "done"
    assert not (settings.get_project_path(project) / "made_by_main").exists()
    _, _, messages = await load_state(project, session_id, parked)
    tool_rows = [m for m in messages if m.role == "tool"]
    assert "not now" in tool_rows[0].content


@pytest.mark.asyncio
async def test_main_loop_resume_re_parks_on_second_gated_call(project, monkeypatch):
    """An approved resume whose continuation hits ANOTHER gated call must
    re-park the resume's own task row: awaiting_input checkpoint present and
    no error event — the second gate starts a fresh approval cycle."""
    script_llm(monkeypatch, [
        script_main_tool_call(),
        script_main_tool_call(tool_call_id="call_main_2"),
        ("text", "done after second approval"),
    ])
    session_id, _ = await make_turn(project)
    task1 = await start_running_task(project, session_id)
    await collect(QueryLoop(project, session_id, task_id=task1))

    task2 = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=task2,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    types = [e["type"] for e in events]
    assert types[-1] == "awaiting_input"
    assert "done" not in types
    assert "error" not in types
    # The first approval did execute before the loop re-parked.
    assert (settings.get_project_path(project) / "made_by_main").is_dir()

    status, interaction, _ = await load_state(project, session_id, task2)
    assert status == "awaiting_input"
    assert interaction["interaction_data"]["interaction_type"] == "permission"
    assert interaction["tool_call_id"] == "call_main_2"
    assert interaction["tool_args"]["command"] == "mkdir -p made_by_main"


@pytest.mark.asyncio
async def test_permission_resume_blocked_when_target_drifted(project, monkeypatch):
    """The approval binds the resolved write target at approval time. If the
    target resolves differently on resume (e.g. a path symlink flipped while
    the task was parked), the approved write must NOT execute: error tool
    result, nothing on disk, and no file_changed event."""
    project_dir = settings.get_project_path(project)
    script_llm(monkeypatch, [
        script_write_tool_call(project_dir), ("text", "done"),
    ])
    session_id, _ = await make_turn(project)
    parked = await start_running_task(project, session_id)
    await collect(QueryLoop(project, session_id, task_id=parked))

    _, interaction, _ = await load_state(project, session_id, parked)
    snapshot = interaction["interaction_data"]["resolved_path"]
    assert snapshot.endswith("made_by_write/note.md")

    # The target moved while the task was parked: re-resolution now lands
    # somewhere else entirely.
    monkeypatch.setattr(
        file_service, "resolve_write_target",
        lambda project_id, path: Path("/elsewhere/target.md"),
    )

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    resume_types = [e["type"] for e in events]
    assert "error" not in resume_types
    assert "file_changed" not in resume_types
    assert not (project_dir / "made_by_write").exists()

    tool_end = next(e for e in events if e["type"] == "tool_end")
    assert "changed since approval" in tool_end["data"]["result_summary"]
    assert "file_edit" not in tool_end["data"]

    _, _, messages = await load_state(project, session_id, parked)
    tool_rows = [m for m in messages if m.role == "tool"]
    assert "was not executed" in tool_rows[0].content
    assert "changed since approval" in tool_rows[0].content


@pytest.mark.asyncio
async def test_permission_resume_refuses_unresolved_target_snapshot(
    project, monkeypatch,
):
    """A write target that cannot be resolved at approval time persists an
    empty resolved_path snapshot. The resume must refuse to execute the
    approved operation — the LLM re-requests it with a resolvable path
    instead of writing to an unvalidated target."""
    project_dir = settings.get_project_path(project)
    real_resolve = file_service.resolve_write_target
    script_llm(monkeypatch, [
        script_write_tool_call(project_dir), ("text", "done"),
    ])
    session_id, _ = await make_turn(project)
    parked = await start_running_task(project, session_id)

    monkeypatch.setattr(
        file_service, "resolve_write_target", lambda *args, **kwargs: None,
    )
    await collect(QueryLoop(project, session_id, task_id=parked))

    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction["interaction_data"]["resolved_path"] == ""

    # The target resolves normally again by resume time; the empty snapshot
    # still refuses execution.
    monkeypatch.setattr(file_service, "resolve_write_target", real_resolve)
    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    resume_types = [e["type"] for e in events]
    assert "error" not in resume_types
    assert "file_changed" not in resume_types
    assert not (project_dir / "made_by_write").exists()

    tool_end = next(e for e in events if e["type"] == "tool_end")
    assert "could not be re-resolved" in tool_end["data"]["result_summary"]
    assert "file_edit" not in tool_end["data"]

    _, _, messages = await load_state(project, session_id, parked)
    tool_rows = [m for m in messages if m.role == "tool"]
    assert "could not be re-resolved" in tool_rows[0].content


@pytest.mark.asyncio
async def test_permission_resume_executes_when_target_unchanged(project, monkeypatch):
    """When the target still resolves to the approval snapshot, the approved
    write executes normally: file created with its content and file_changed
    emitted for the frontend tree refresh."""
    project_dir = settings.get_project_path(project)
    script_llm(monkeypatch, [
        script_write_tool_call(project_dir), ("text", "done"),
    ])
    session_id, _ = await make_turn(project)
    parked = await start_running_task(project, session_id)
    await collect(QueryLoop(project, session_id, task_id=parked))

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    assert "error" not in [e["type"] for e in events]
    assert any(e["type"] == "file_changed" for e in events)
    target = project_dir / "made_by_write" / "note.md"
    assert target.is_file()
    assert target.read_text() == "body"

    _, _, messages = await load_state(project, session_id, parked)
    assert messages[-1].content == "done"


def _write_analysis_notebook(project, source):
    notebook = {
        "cells": [{
            "cell_type": "code", "id": "c1", "metadata": {},
            "source": source, "outputs": [], "execution_count": None,
        }],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }
    path = settings.get_project_path(project) / "analysis.ipynb"
    path.write_text(json.dumps(notebook))
    return path


async def _park_notebook_run(project, monkeypatch):
    """Drive a real notebook_run_cell pause: notebook_read satisfies the
    must-read preflight, then the gated run parks with a cell-source digest
    in the checkpoint."""
    script_llm(monkeypatch, [
        script_notebook_read_call(),
        script_notebook_run_call(),
        ("text", "done"),
    ])
    # make_turn only seeds the session with the user row; the run under test
    # is the harness's own task row, promoted to running before the loop starts.
    session_id, _ = await make_turn(project)
    parked = await start_running_task(project, session_id)
    await collect(QueryLoop(project, session_id, task_id=parked))
    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction["tool_name"] == "notebook_run_cell"
    return session_id, parked, interaction


@pytest.mark.asyncio
async def test_edit_resume_after_restart_executes_and_new_writes_stay_gated(
    project, monkeypatch,
):
    """Restart simulation: an approved edit of an EXISTING file resumes with
    an empty read-state cache (the cache is in-memory; the checkpoint is not).
    The approved operation must still execute — the pause implies the file was
    read pre-pause — while a NEW write the model issues in the resumed loop
    against a never-read file stays rejected by the must-read gate."""
    project_dir = settings.get_project_path(project)
    (project_dir / "report.md").write_text("alpha\n")
    (project_dir / "other.md").write_text("untouched\n")

    script_llm(monkeypatch, [
        script_read_tool_call(project_dir),
        script_edit_tool_call(project_dir),
        ("tools", [{
            "id": "call_new_write", "name": "write",
            "params": {
                "file_path": str(project_dir / "other.md"),
                "content": "sneaky",
            },
        }]),
        ("text", "resumed done"),
    ])
    session_id, _ = await make_turn(project)
    parked = await start_running_task(project, session_id)
    await collect(QueryLoop(project, session_id, task_id=parked))

    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction["tool_name"] == "edit"

    # The restart boundary: the in-memory read-state cache does not survive
    # it, the persisted checkpoint does.
    read_state_cache.clear(session_id)

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" not in types
    # The approved edit executed across the restart.
    assert (project_dir / "report.md").read_text() == "beta\n"
    # The model's new write to a never-read file was gated, not executed.
    assert (project_dir / "other.md").read_text() == "untouched\n"
    _, _, messages = await load_state(project, session_id, parked)
    tool_rows = [m for m in messages if m.role == "tool"]
    assert any("has not been read" in m.content for m in tool_rows)
    assert messages[-1].content == "resumed done"


@pytest.mark.asyncio
async def test_notebook_resume_refuses_cell_changed_since_approval(
    project, monkeypatch,
):
    """The approval binds the cell's FULL source via its sha256 snapshot. A
    cell edited while the task was parked must not run: the resume injects
    the not-executed error instead of executing the new code the user never
    saw."""
    _write_analysis_notebook(project, "print('approved')")
    session_id, parked, interaction = await _park_notebook_run(project, monkeypatch)
    assert interaction["interaction_data"]["content_sha256"] == (
        hashlib.sha256("print('approved')".encode("utf-8")).hexdigest()
    )

    _write_analysis_notebook(project, "print('tampered')")

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" not in types
    assert "file_changed" not in types
    tool_end = next(e for e in events if e["type"] == "tool_end")
    assert "changed since approval" in tool_end["data"]["result_summary"]
    _, _, messages = await load_state(project, session_id, parked)
    tool_rows = [m for m in messages if m.role == "tool"]
    assert any("not executed" in m.content for m in tool_rows)


class _FakeJupyterService:
    """In-memory jupyter daemon stand-in: a running server with one idle
    kernel whose execute_code records the cell source it is asked to run."""

    def __init__(self):
        self.executed = []

    async def is_running(self):
        return True

    async def get_session_for_notebook(self, notebook_path, create=True):
        return {"kernel": {"id": "kernel-1"}}

    async def get_kernel_status(self, kernel_id):
        return {"execution_state": "idle"}

    async def execute_code(self, kernel_id, source, timeout=60.0):
        self.executed.append(source)
        return {"status": "ok", "outputs": [], "execution_count": 1}


def _mock_jupyter(monkeypatch):
    """Replace the notebook tool's jupyter dependency so the approved run's
    contract is pinned without a real Jupyter server."""
    fake = _FakeJupyterService()
    monkeypatch.setattr(
        "app.agents.tools.notebook_tools.get_jupyter", lambda: fake,
    )
    return fake


@pytest.mark.asyncio
async def test_notebook_resume_executes_when_cell_unchanged(project, monkeypatch):
    """An unchanged cell passes the digest check and the approved run reaches
    the kernel: the notebook tool's jupyter dependency is mocked, so the only
    contract under test is the approval replay gate."""
    _write_analysis_notebook(project, "print('approved')")
    session_id, parked, _ = await _park_notebook_run(project, monkeypatch)
    fake_jupyter = _mock_jupyter(monkeypatch)

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" not in types
    tool_end = next(e for e in events if e["type"] == "tool_end")
    summary = tool_end["data"]["result_summary"]
    assert "not executed" not in summary
    assert "changed since approval" not in summary
    # The approved run actually executed the approved cell source.
    assert fake_jupyter.executed == ["print('approved')"]


@pytest.mark.asyncio
async def test_notebook_resume_executes_pre_upgrade_checkpoint_without_digest(
    project, monkeypatch,
):
    """A checkpoint persisted before the digest existed carries no
    content_sha256; the resume must execute it exactly as before the
    upgrade instead of failing closed."""
    _write_analysis_notebook(project, "print('approved')")
    session_id, parked, _ = await _park_notebook_run(project, monkeypatch)
    fake_jupyter = _mock_jupyter(monkeypatch)

    from app.database.models import TaskState
    async with UnitOfWork(project) as uow:
        row = await uow.task_state.get_by_id(parked)
        payload = row["interaction_state"]
        del payload["interaction_data"]["content_sha256"]
        await uow.session.execute(
            update(TaskState)
            .where(TaskState.task_id == parked)
            .values(interaction_state=json.dumps(payload))
        )
        await uow.commit()

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" not in types
    tool_end = next(e for e in events if e["type"] == "tool_end")
    summary = tool_end["data"]["result_summary"]
    assert "not executed" not in summary
    # The legacy checkpoint ran exactly as before the digest existed.
    assert fake_jupyter.executed == ["print('approved')"]


@pytest.mark.asyncio
async def test_permission_deny_reason_truncated_in_persisted_denial(
    project, monkeypatch,
):
    """The denial reason is user free text that lands in the persisted
    message history: a reason beyond the approval-payload soft limit is
    truncated (marker kept) instead of parking unbounded text in history."""
    script_llm(monkeypatch, [script_main_tool_call(), ("text", "understood")])
    session_id, _ = await make_turn(project)
    parked = await start_running_task(project, session_id)
    await collect(QueryLoop(project, session_id, task_id=parked))

    reason = "because" * (CONTENT_SOFT_LIMIT // 7 + 1)
    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": False, "reason": reason}),
    ))

    assert events[-1]["type"] == "done"
    _, _, messages = await load_state(project, session_id, parked)
    denial = next(m.content for m in messages if m.role == "tool")
    assert "User rejected to execute this command" in denial
    assert ("User says: " + "because" * 10) in denial
    assert "[truncated]" in denial
    assert reason not in denial


@pytest.mark.asyncio
async def test_parallel_batch_pause_resume_completes(project, monkeypatch):
    """A permission pause inside a parallel tool batch checkpoints the
    assistant message with unanswered sibling calls. The resume must run the
    approved call, then execute the still-unanswered siblings through the
    runner's entry continuation (nothing silently dropped) and finish the
    turn with a fully paired history — never surface the provider's pairing
    rejection ("No tool output found for function call ...") as a failure."""
    calls = script_llm(monkeypatch, [
        script_parallel_batch(),
        ("text", "resumed after approval"),
    ])
    session_id, parked = await make_turn(project)

    events = await collect(QueryLoop(project, session_id, task_id=parked))

    types = [e["type"] for e in events]
    assert types[-1] == "awaiting_input"
    assert "done" not in types
    assert "error" not in types

    status, interaction, messages = await load_state(project, session_id, parked)
    assert status == "awaiting_input"
    assert interaction["tool_call_id"] == "call_main"
    # The checkpoint holds the batch with NO tool results — the pause fired
    # on the batch's first call.
    roles = [(m.role, bool(m.tool_calls)) for m in messages]
    assert roles == [("user", False), ("assistant", True)]

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    resume_types = [e["type"] for e in resume_events]
    assert resume_types[-1] == "done"
    assert "error" not in resume_types
    # The approved call executes first (resume path), then the runner entry
    # completes the pending round: every sibling runs, in batch order.
    tool_ends = [e for e in resume_events if e["type"] == "tool_end"]
    assert [e["data"]["tool_call_id"] for e in tool_ends] == [
        "call_main", *[f"call_sib_{i}" for i in range(4)],
    ]
    assert (settings.get_project_path(project) / "made_by_main").is_dir()

    # The resume's LLM call carries the batch FULLY answered: the approved
    # call's result plus every sibling's result from the continuation.
    resume_messages = calls["messages"][1]
    batch = next(
        m for m in resume_messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    sent_ids = {tc["id"] for tc in batch["tool_calls"]}
    assert sent_ids == {"call_main", *[f"call_sib_{i}" for i in range(4)]}
    answered = {
        m.get("tool_call_id") for m in resume_messages if m.get("role") == "tool"
    }
    assert answered == {"call_main", *[f"call_sib_{i}" for i in range(4)]}

    status, _, messages = await load_state(project, session_id, parked)
    assert status == "completed"
    tool_rows = [m for m in messages if m.role == "tool"]
    assert len(tool_rows) == 5


@pytest.mark.asyncio
async def test_subagent_permission_pause_parks_task_not_fails(project, monkeypatch):
    """A general subagent's bash pause checkpoints the task as a subagent
    interaction — never terminal-fails it."""
    script_llm(monkeypatch, [script_agent_tool_call(), script_subagent_bash()])
    session_id, task_id = await make_turn(project)

    events = await collect(QueryLoop(project, session_id, task_id=task_id))

    types = [e["type"] for e in events]
    assert types[-1] == "awaiting_input"
    assert "done" not in types
    assert "error" not in types

    status, interaction, messages = await load_state(project, session_id, task_id)
    assert status == "awaiting_input"
    assert interaction["is_subagent_interaction"] is True
    assert interaction["agent_session_id"]
    assert interaction["parent_tool_call_id"] == "call_agent"
    assert interaction["interaction_data"]["interaction_type"] == "permission"
    assert interaction["interaction_data"]["task_id"] == task_id
    assert interaction["inner_tool_args"]["command"] == "mkdir -p made_by_agent"
    roles = [(m.role, bool(m.tool_calls)) for m in messages]
    assert roles == [("user", False), ("assistant", True)]

    async with UnitOfWork(project) as uow:
        agent_session = await uow.sessions.get_agent_session(
            interaction["agent_session_id"], agent_type="general",
        )
        assert agent_session is not None
        agent_messages = await uow.messages.get_messages(agent_session.id)
    agent_roles = [(m.role, bool(m.tool_calls)) for m in agent_messages]
    # The subagent's bash tool_call row is persisted before the pause.
    assert ("assistant", True) in agent_roles


@pytest.mark.asyncio
async def test_subagent_permission_approve_resume_continues_subagent(project, monkeypatch):
    script_llm(monkeypatch, [
        script_agent_tool_call(),
        script_subagent_bash(),
        ("text", "subagent done"),
        ("text", "main done"),
    ])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    assert resume_events[-1]["type"] == "done"
    assert "error" not in [e["type"] for e in resume_events]
    assert (settings.get_project_path(project) / "made_by_agent").is_dir()

    parked_status, interaction, messages = await load_state(project, session_id, parked)
    assert parked_status == "completed"
    assert interaction is None
    agent_tool_rows = [
        m for m in messages if m.role == "tool" and m.tool_call_id == "call_agent"
    ]
    assert len(agent_tool_rows) == 1
    assert "main done" == messages[-1].content


@pytest.mark.asyncio
async def test_subagent_permission_resume_blocked_when_target_drifted(project, monkeypatch):
    """The approval's target snapshot binds the subagent's write the same way
    it binds a direct one: if the resolved target moved while the task was
    parked, approval must not execute it. The drift error is injected as the
    inner tool result so the subagent can continue from the refusal."""
    project_dir = settings.get_project_path(project)
    script_llm(monkeypatch, [
        script_agent_tool_call(),
        script_subagent_write(project_dir),
        ("text", "subagent done"),
        ("text", "main done"),
    ])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))

    _, interaction, _ = await load_state(project, session_id, parked)
    snapshot = interaction["inner_interaction_data"]["resolved_path"]
    assert snapshot.endswith("made_by_agent_write/note.md")

    monkeypatch.setattr(
        file_service, "resolve_write_target",
        lambda project_id, path: Path("/elsewhere/target.md"),
    )

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    resume_types = [e["type"] for e in resume_events]
    assert resume_types[-1] == "done"
    assert "error" not in resume_types
    assert "file_changed" not in resume_types
    assert not (project_dir / "made_by_agent_write").exists()

    inner_end = next(
        e for e in resume_events
        if e["type"] == "agent_event" and e["data"]["inner_type"] == "tool_end"
    )
    assert "changed since approval" in inner_end["data"]["inner_data"]["result_summary"]
    assert "file_edit" not in inner_end["data"]["inner_data"]

    # The subagent saw the refusal, finished, and the main turn completed.
    _, interaction, messages = await load_state(project, session_id, parked)
    assert interaction is None
    assert messages[-1].content == "main done"


@pytest.mark.asyncio
async def test_subagent_permission_resume_executes_when_target_unchanged(project, monkeypatch):
    """An unchanged snapshot on a subagent's approved write executes it:
    file created and file_changed emitted for the frontend tree refresh."""
    project_dir = settings.get_project_path(project)
    script_llm(monkeypatch, [
        script_agent_tool_call(),
        script_subagent_write(project_dir),
        ("text", "subagent done"),
        ("text", "main done"),
    ])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    assert "error" not in [e["type"] for e in resume_events]
    assert any(e["type"] == "file_changed" for e in resume_events)
    target = project_dir / "made_by_agent_write" / "note.md"
    assert target.is_file()
    assert target.read_text() == "agent body"

    inner_end = next(
        e for e in resume_events
        if e["type"] == "agent_event" and e["data"]["inner_type"] == "tool_end"
    )
    assert "file_edit" in inner_end["data"]["inner_data"]

    _, _, messages = await load_state(project, session_id, parked)
    assert messages[-1].content == "main done"


@pytest.mark.asyncio
async def test_fork_agent_permission_approval_carries_file_edit_metadata(
    project, monkeypatch,
):
    """A fork agent's approved write forwards the inner tool's file-edit
    metadata on the outer agent tool_end — the same checkpoint-derived
    metadata the persistent-subagent path emits for its inner tool_end."""
    project_dir = settings.get_project_path(project)
    script_llm(monkeypatch, [
        script_fork_agent_tool_call(),
        script_write_tool_call(project_dir, tool_call_id="call_fork_write"),
        ("text", "main done"),
    ])
    session_id, parked = await make_turn(project)

    events = await collect(QueryLoop(project, session_id, task_id=parked))
    assert events[-1]["type"] == "awaiting_input"
    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction["tool_name"] == "agent"
    assert interaction["inner_tool_name"] == "write"

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    assert "error" not in [e["type"] for e in resume_events]
    target = project_dir / "made_by_write" / "note.md"
    assert target.is_file()
    assert target.read_text() == "body"

    tool_end = next(
        e for e in resume_events
        if e["type"] == "tool_end" and e["data"]["tool_call_id"] == "call_agent"
    )
    edit_meta = tool_end["data"]["file_edit"]
    assert edit_meta["kind"] == "write"
    assert edit_meta["path"] == str(target)


@pytest.mark.asyncio
async def test_fork_interactive_pause_parks_recoverable_checkpoint(project, monkeypatch):
    """A fork subagent's ask_user_question parks the turn with the
    direct-style checkpoint (tool_name is the outer agent call plus the
    inner tool's identity) — never the is_subagent_interaction shape, whose
    empty agent_session_id resume cannot recover and would drop the
    question."""
    script_llm(monkeypatch, [
        script_fork_agent_tool_call(),
        script_fork_ask_user(),
        ("text", "main done"),
    ])
    session_id, task_id = await make_turn(project)

    events = await collect(QueryLoop(project, session_id, task_id=task_id))

    types = [e["type"] for e in events]
    assert types[-1] == "awaiting_input"
    assert "done" not in types and "error" not in types
    agent_event = next(e for e in events if e["type"] == "agent_event")
    assert agent_event["data"]["parent_tool_call_id"] == "call_agent"

    status, interaction, messages = await load_state(project, session_id, task_id)
    assert status == "awaiting_input"
    assert "is_subagent_interaction" not in interaction
    assert interaction["tool_name"] == "agent"
    assert interaction["tool_call_id"] == "call_agent"
    assert interaction["inner_tool_name"] == "ask_user_question"
    assert interaction["inner_tool_args"]["questions"][0]["question"] == "Which database?"
    assert interaction["interaction_data"]["interaction_type"] == "ask_user_question"
    roles = [(m.role, bool(m.tool_calls)) for m in messages]
    assert roles == [("user", False), ("assistant", True)]


@pytest.mark.asyncio
async def test_fork_interactive_resume_injects_answer_as_agent_result(
    project, monkeypatch,
):
    """Answering a fork subagent's question executes the inner interactive
    tool with the response and injects the result as the outer agent tool's
    result (tool_call_id = parent call), then the main loop re-runs — the
    same injection contract as the fork permission fallback."""
    script_llm(monkeypatch, [
        script_fork_agent_tool_call(),
        script_fork_ask_user(),
        ("text", "main done"),
    ])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"answers": [
            {"question": "Which database?", "answer": "Postgres"},
        ]}),
    ))

    resume_types = [e["type"] for e in resume_events]
    assert resume_types[-1] == "done"
    assert "error" not in resume_types
    tool_end = next(
        e for e in resume_events
        if e["type"] == "tool_end" and e["data"]["tool_call_id"] == "call_agent"
    )
    assert "Postgres" in tool_end["data"]["result_summary"]

    status, interaction, messages = await load_state(project, session_id, parked)
    assert status == "completed"
    assert interaction is None
    agent_rows = [
        m for m in messages if m.role == "tool" and m.tool_call_id == "call_agent"
    ]
    assert len(agent_rows) == 1
    assert "User Answer: Postgres" in agent_rows[0].content
    assert messages[-1].content == "main done"


@pytest.mark.asyncio
async def test_subagent_plan_approval_resume_uses_shared_merge_contract(
    project, monkeypatch,
):
    """The subagent interactive resume runs the same merge contract as the
    direct path: the plan is saved under the MAIN session (the caller-chosen
    session re-stamp), and a response cannot swap the checkpointed
    plan_content — the modal's approval decision still arrives."""
    script_llm(monkeypatch, [
        script_plan_agent_tool_call(),
        script_plan_submission(),
        ("text", "main done"),
    ])
    session_id, parked = await make_turn(project)

    events = await collect(QueryLoop(project, session_id, task_id=parked))
    assert events[-1]["type"] == "awaiting_input"
    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction["is_subagent_interaction"] is True
    assert interaction["agent_type"] == "plan"
    assert interaction["inner_tool_name"] == "submit_plan_for_approval"
    agent_session_id = interaction["agent_session_id"]

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {
            "approved": True, "plan_content": "# Swapped at approval time",
        }),
    ))

    resume_types = [e["type"] for e in resume_events]
    assert resume_types[-1] == "done"
    assert "error" not in resume_types

    plans_dir = (
        settings.get_project_path(project) / ".SiGMA" / "sessions"
        / session_id / "plans"
    )
    saved = list(plans_dir.glob("*.md"))
    assert len(saved) == 1
    assert saved[0].read_text() == "# The real plan"

    # The plan was NOT saved under the agent session that checkpointed the
    # tool call — the re-stamp targets the main session.
    agent_plans_dir = (
        settings.get_project_path(project) / ".SiGMA" / "sessions"
        / agent_session_id / "plans"
    )
    assert not agent_plans_dir.exists()

    _, _, messages = await load_state(project, session_id, parked)
    assert messages[-1].content == "main done"


@pytest.mark.asyncio
async def test_subagent_resume_fork_new_permission_pause_recheckpoints(project, monkeypatch):
    """While resuming a subagent interaction, the main loop may fork ANOTHER
    agent whose tool hits a permission gate. That pause must be
    re-checkpointed as awaiting_input — it must never escape the resume path
    and terminal-fail the streaming task."""
    script_llm(monkeypatch, [
        script_agent_tool_call(),
        script_subagent_bash(),
        ("text", "subagent done"),
        script_agent_tool_call(tool_call_id="call_agent_2"),
        script_subagent_bash(
            tool_call_id="call_sub_bash_2", command="mkdir -p made_by_agent_2",
        ),
        ("text", "subagent two done"),
        ("text", "main done"),
    ])
    session_id, _ = await make_turn(project)

    # Phase 1 — first turn: fork a general subagent whose bash pauses.
    task1 = await start_running_task(project, session_id)
    events = await collect(QueryLoop(project, session_id, task_id=task1))
    types = [e["type"] for e in events]
    assert types[-1] == "awaiting_input"
    assert "done" not in types
    assert "error" not in types
    status, interaction, _ = await load_state(project, session_id, task1)
    assert status == "awaiting_input"
    assert interaction["is_subagent_interaction"] is True

    # Phase 2 — first resume: the paused subagent finishes, then the main
    # loop forks a second agent whose bash hits the gate again; the new
    # pause must re-park the task instead of failing it.
    task2 = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=task2,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))
    resume_types = [e["type"] for e in resume_events]
    assert resume_types[-1] == "awaiting_input"
    assert "done" not in resume_types
    assert "error" not in resume_types

    status, interaction, _ = await load_state(project, session_id, task2)
    assert status == "awaiting_input"
    assert interaction["is_subagent_interaction"] is True
    assert interaction["parent_tool_call_id"] == "call_agent_2"
    assert interaction["inner_tool_args"]["command"] == "mkdir -p made_by_agent_2"

    # Phase 3 — second resume: the second subagent finishes; the turn completes.
    task3 = await start_running_task(project, session_id)
    resume2_events = await collect(QueryLoop(
        project, session_id, task_id=task3,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))
    resume2_types = [e["type"] for e in resume2_events]
    assert resume2_types[-1] == "done"
    assert "error" not in resume2_types

    # Terminal status marking is the runner's job (task_runtime finalizes the
    # row after the stream ends; this harness calls QueryLoop directly). The loop-level
    # contract is: no pending interaction left, both approved operations
    # executed, and the turn's final text persisted.
    status, interaction, messages = await load_state(project, session_id, task3)
    assert interaction is None
    assert (settings.get_project_path(project) / "made_by_agent").is_dir()
    assert (settings.get_project_path(project) / "made_by_agent_2").is_dir()
    assert messages[-1].content == "main done"


# Subagent permission resume: checkpoint claim ordering
# ---------------------------------------------------------------------------


async def park_subagent_write_pause(project, monkeypatch, project_dir):
    """Run a real loop until a subagent write pauses on its permission gate."""
    script_llm(monkeypatch, [
        script_agent_tool_call(),
        script_subagent_write(project_dir),
    ])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))
    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction is not None
    assert interaction["is_subagent_interaction"] is True
    return session_id, parked


@pytest.mark.asyncio
async def test_subagent_resume_after_checkpoint_consumed_prevents_execution(
    project, monkeypatch,
):
    """A cancel consumes the parked checkpoint before the resume arrives:
    the approved operation must NOT execute — the claim's rowcount gates the
    side effect, matching the direct-permission resume contract."""
    project_dir = settings.get_project_path(project)
    session_id, parked = await park_subagent_write_pause(
        project, monkeypatch, project_dir,
    )
    response = await owned_response(project, session_id, {"approved": True})

    # request_cancel on an awaiting_input row consumes the checkpoint.
    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(parked)

    executed = []

    async def counting_call_tool(tool_def, tool_args):
        executed.append(tool_def.name)
        return "must not run"

    monkeypatch.setattr(
        LLMLoopRunner, "call_tool", staticmethod(counting_call_tool),
    )

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=response,
    ))

    assert [e["type"] for e in events] == ["done"]
    assert executed == []
    assert not (project_dir / "made_by_agent_write").exists()


@pytest.mark.asyncio
async def test_double_subagent_permission_resume_executes_tool_once(
    project, monkeypatch,
):
    """Two approvals against the same parked subagent checkpoint execute the
    approved operation exactly once: the loser of the claim ends cleanly."""
    project_dir = settings.get_project_path(project)
    script_llm(monkeypatch, [
        script_agent_tool_call(),
        script_subagent_write(project_dir),
        ("text", "subagent done"),
        ("text", "main done"),
    ])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))
    response = await owned_response(project, session_id, {"approved": True})

    executed = []
    real_call_tool = LLMLoopRunner.call_tool

    async def counting_then_real(tool_def, tool_args):
        executed.append(tool_def.name)
        return await real_call_tool(tool_def, tool_args)

    monkeypatch.setattr(
        LLMLoopRunner, "call_tool", staticmethod(counting_then_real),
    )

    resume_task = await start_running_task(project, session_id)
    resume_events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=response,
    ))
    assert "error" not in [e["type"] for e in resume_events]
    assert (project_dir / "made_by_agent_write" / "note.md").is_file()
    assert executed.count("write") == 1

    second_task = await start_running_task(project, session_id)
    second_events = await collect(QueryLoop(
        project, session_id, task_id=second_task,
        interaction_response=response,
    ))

    assert [e["type"] for e in second_events] == ["done"]
    assert executed.count("write") == 1  # no second execution
