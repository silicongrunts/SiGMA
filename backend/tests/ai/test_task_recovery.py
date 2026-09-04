"""Task recovery: startup reconciliation, stale/corrupt checkpoints, and
duplicate or cancelled resumes.

Whatever survives an interrupted run must stay recoverable — startup
reconcile fails stranded rows without touching parked checkpoints, broken
checkpoints keep their identity and report interaction_failed, duplicate
resumes end silently instead of re-executing, and a cancel beats an
approval to the checkpoint.
"""

import asyncio
import contextlib
from importlib import import_module

import pytest
from sqlalchemy import update

import_module("app.agents.tools")
from app.core.exceptions import TaskActiveError
from app.database.unit_of_work import UnitOfWork
from app.services import task_runtime
from app.services.ai_service import ai_service
from app.services.llm_loop_runner import LLMLoopRunner
from app.services.query_loop import QueryLoop
from tests.ai.matrix_harness import (  # noqa: F401 (autouse fixture below)
    collect,
    load_state,
    load_state_row,
    make_turn,
    owned_response,
    park_permission_checkpoint,
    script_llm,
    script_main_tool_call,
    start_running_task,
    use_fixture_project_root,
)


@pytest.mark.asyncio
async def test_startup_reconcile_fails_interrupted_rows_and_session_resubmits(
    project, monkeypatch,
):
    """A queued row left behind by a run that did not survive a restart is
    failed by startup reconciliation with the restart reason, and the freed
    session then accepts a fresh submission."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, stale_id = await make_turn(project)

    failed = await task_runtime.startup_reconcile()

    assert failed == 1
    stale = await load_state_row(project, stale_id)
    assert stale["status"] == "failed"
    assert stale["error"] == "Task was interrupted by an application restart."

    failed = await task_runtime.startup_reconcile()
    assert failed == 0  # idempotent

    result = await ai_service.submit_chat(project, "after restart", {}, session_id=session_id)
    fresh = await load_state_row(project, result["task_id"])
    assert fresh["status"] == "queued"
    # Exactly one streaming task launched, into the project's session, and
    # its id matches the fresh submission result.
    assert len(launched) == 1
    assert launched[0]["task_id"] == result["task_id"]
    assert launched[0]["project_id"] == project


@pytest.mark.asyncio
async def test_startup_reconcile_preserves_parked_checkpoint(project):
    """A parked interaction checkpoint survives startup reconciliation: the
    interrupted resume row is failed, but the awaiting_input row stays put
    and keeps guarding the session against new submissions."""
    session_id, parked_id = await make_turn(project)
    await park_permission_checkpoint(project, session_id, parked_id)
    running_id = await start_running_task(project, session_id)

    failed = await task_runtime.startup_reconcile()

    assert failed == 1
    parked = await load_state_row(project, parked_id)
    assert parked["status"] == "awaiting_input"
    interrupted = await load_state_row(project, running_id)
    assert interrupted["status"] == "failed"

    with pytest.raises(TaskActiveError):
        await ai_service.submit_chat(project, "new message", {}, session_id=session_id)


@pytest.mark.asyncio
async def test_duplicate_resume_after_checkpoint_consumed_ends_silently(project, monkeypatch):
    """A second resume after the checkpoint was already consumed ends with
    a lone done event — no error event, no phantom error message persisted
    into history. A stale approval is not a task failure."""
    script_llm(monkeypatch, [
        script_main_tool_call(),
        ("text", "dir created"),
    ])
    session_id, task_id = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=task_id))

    resume_task = await start_running_task(project, session_id)
    response = await owned_response(project, session_id, {"approved": True})
    await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=response,
    ))
    _, _, messages_after_resume = await load_state(project, session_id, resume_task)
    assert messages_after_resume[-1].content == "dir created"

    duplicate_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=duplicate_task,
        interaction_response=response,
    ))

    assert [e["type"] for e in events] == ["done"]
    _, _, messages = await load_state(project, session_id, duplicate_task)
    assert len(messages) == len(messages_after_resume)
    assert messages[-1].content == "dir created"


# ---------------------------------------------------------------------------
# /compact cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_during_compact_aborts_promptly_and_finalizes_cancelled(
    project, monkeypatch,
):
    """A user stop during /compact must abort the in-flight compaction LLM
    call instead of letting it run to completion: the row ends cancelled and
    no done/compaction result is streamed afterwards."""
    import app.services.compaction_service as compaction_module
    from app.core.task_status import STATUS_CANCELLED

    entered = asyncio.Event()
    aborted = []
    gate = asyncio.Event()

    async def blocking_call_chat_text(**kwargs):
        entered.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            aborted.append(True)
            raise
        return ("a" * 80, None)

    monkeypatch.setattr(
        compaction_module.llm_service, "call_chat_text", blocking_call_chat_text,
    )

    async with UnitOfWork(project) as uow:
        session = await uow.sessions.create()

    result = await ai_service.submit_chat(project, "/compact", {}, session_id=session.id)
    task_id = result["task_id"]

    await asyncio.wait_for(entered.wait(), timeout=10)
    cancel_result = await ai_service.cancel_task(project, task_id)
    assert cancel_result["cancelled"] is True

    # The runner's CancelledError handler finalizes the row and re-raises, so
    # the runner task ends in the cancelled state.
    runner_task = task_runtime._tasks[task_id]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(runner_task, timeout=10)

    row = await load_state_row(project, task_id)
    assert row["status"] == STATUS_CANCELLED
    assert aborted == [True]  # the in-flight compaction call was cancelled


# ---------------------------------------------------------------------------
# Stuck-checkpoint recovery: failed resumes clear the parked checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_subagent_checkpoint_cleared_and_session_resubmits(
    project, monkeypatch,
):
    """An invalid subagent checkpoint remains recoverable after failure."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, parked = await make_turn(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_awaiting_input(parked, {
            "is_subagent_interaction": True,
            "agent_session_id": "",
            "parent_tool_call_id": "",
            "interaction_data": {"interaction_type": "permission"},
            "checkpoint": {
                "task_id": parked,
                "interaction_id": f"interaction-{parked}",
                "interaction_type": "permission",
            },
        })
    response = await owned_response(project, session_id, {"approved": True})

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=response,
    ))

    assert events[-1]["type"] == "error"
    assert "Invalid subagent checkpoint" in events[-1]["data"]["error"]
    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction["checkpoint"]["interaction_id"] == f"interaction-{parked}"
    assert (await load_state_row(project, parked))["status"] == "interaction_failed"

    # Finalize the resume's own row (the harness drives QueryLoop directly,
    # so no runner finalizes it), then the session accepts a fresh submit.
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(resume_task)
    with pytest.raises(TaskActiveError):
        await ai_service.submit_chat(project, "again", {}, session_id=session_id)


@pytest.mark.asyncio
async def test_permission_resume_with_failed_message_build_preserves_checkpoint(
    project, monkeypatch,
):
    """A message-build failure retains the approval checkpoint for recovery."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, parked = await make_turn(project)
    await park_permission_checkpoint(project, session_id, parked)

    async def empty_build(self):
        return []

    monkeypatch.setattr(QueryLoop, "_build_messages", empty_build)

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}),
    ))

    assert events[-1]["type"] == "error"
    assert "Failed to load checkpoint" in events[-1]["data"]["error"]
    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction["checkpoint"]["interaction_id"] == f"interaction-{parked}"
    assert (await load_state_row(project, parked))["status"] == "interaction_failed"

    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(resume_task)
    with pytest.raises(TaskActiveError):
        await ai_service.submit_chat(project, "again", {}, session_id=session_id)


@pytest.mark.asyncio
async def test_stale_noninteractive_checkpoint_preserves_recovery_identity(
    project, monkeypatch,
):
    """A stale tool checkpoint retains identity and reports a recoverable error."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, parked = await make_turn(project)
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_awaiting_input(parked, {
            "tool_name": "read",  # not interactive
            "tool_call_id": "call_x",
            "tool_args": {},
            "interaction_data": {"interaction_type": "ask_user_question"},
            "checkpoint": {
                "task_id": parked,
                "interaction_id": f"interaction-{parked}",
                "interaction_type": "ask_user_question",
            },
        })
    response = await owned_response(project, session_id, {"answers": []})

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=response,
    ))

    assert events[-1]["type"] == "error"
    _, interaction, _ = await load_state(project, session_id, parked)
    assert interaction["checkpoint"]["interaction_id"] == f"interaction-{parked}"
    assert (await load_state_row(project, parked))["status"] == "interaction_failed"

    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_completed(resume_task)
    with pytest.raises(TaskActiveError):
        await ai_service.submit_chat(project, "again", {}, session_id=session_id)


@pytest.mark.asyncio
async def test_corrupt_checkpoint_self_heals_and_session_resubmits(project, monkeypatch):
    """A checkpoint row whose interaction_state JSON is corrupt can neither
    render a dialog nor resume, yet it refuses every new submit (409) with
    no recovery path. The repo reads self-heal by deleting the stale row:
    getActive reports no active task and the session accepts submissions."""
    launched = []
    monkeypatch.setattr(task_runtime, "launch", lambda **kw: launched.append(kw))
    session_id, parked = await make_turn(project)
    await park_permission_checkpoint(project, session_id, parked)
    from app.database.models import TaskState

    async with UnitOfWork(project) as uow:
        await uow.session.execute(
            update(TaskState)
            .where(TaskState.task_id == parked)
            .values(interaction_state='{"tool_name": ')  # truncated JSON
        )
        await uow.commit()

    active = await ai_service.get_active_task(project, session_id)

    assert active == {"active": False, "task_id": None, "status": None}

    result = await ai_service.submit_chat(project, "again", {}, session_id=session_id)

    assert len(launched) == 1
    row = await load_state_row(project, result["task_id"])
    assert row["status"] == "queued"
    stale = await load_state_row(project, parked)
    assert stale is None


# ---------------------------------------------------------------------------
# Direct interactive-tool resume: claim ordering and idempotency
# ---------------------------------------------------------------------------


async def park_ask_user_checkpoint(project, session_id, task_id, tool_call_id):
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_awaiting_input(task_id, {
            "tool_name": "ask_user_question",
            "tool_call_id": tool_call_id,
            "tool_args": {"questions": []},
            "interaction_data": {"interaction_type": "ask_user_question"},
            "checkpoint": {
                "task_id": task_id,
                "interaction_id": f"interaction-{task_id}",
                "interaction_type": "ask_user_question",
            },
        })


@pytest.mark.asyncio
async def test_double_direct_resume_executes_interactive_tool_once(
    project, monkeypatch,
):
    """Two resumes answering the same parked interactive checkpoint execute
    the tool exactly once: the first resume's claim consumes the checkpoint
    and persists the result, the second ends silently off history alone."""
    executions = []

    async def counting_execute(tool_def, tool_args, response):
        executions.append(response)
        return "formatted answers"

    monkeypatch.setattr(
        LLMLoopRunner, "call_interactive_tool", staticmethod(counting_execute),
    )
    script_llm(monkeypatch, [("text", "acknowledged")])

    session_id, parked = await make_turn(project)
    await park_ask_user_checkpoint(project, session_id, parked, "call_q")
    response = await owned_response(project, session_id, {"answers": [{"question": "q?", "answer": "yes"}]})

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=response,
    ))

    assert "error" not in [e["type"] for e in events]
    assert len(executions) == 1
    _, _, messages = await load_state(project, session_id, parked)
    tool_rows = [m for m in messages if m.role == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0].tool_call_id == "call_q"
    assert tool_rows[0].content == "formatted answers"
    assert messages[-1].content == "acknowledged"

    second_task = await start_running_task(project, session_id)
    second_events = await collect(QueryLoop(
        project, session_id, task_id=second_task,
        interaction_response=response,
    ))

    assert [e["type"] for e in second_events] == ["done"]
    assert len(executions) == 1  # the second resume re-executed nothing


@pytest.mark.asyncio
async def test_cancel_before_direct_resume_prevents_execution(project, monkeypatch):
    """A cancel consumes the parked checkpoint before the resume arrives:
    the interactive tool must NOT execute on an already-consumed checkpoint
    — losing the answer is safer than a duplicated side effect."""
    session_id, parked = await make_turn(project)
    await park_ask_user_checkpoint(project, session_id, parked, "call_q")
    response = await owned_response(project, session_id, {"answers": [{"question": "q?", "answer": "yes"}]})

    async with UnitOfWork(project) as uow:
        await uow.task_state.request_cancel(parked)

    async def must_not_execute(tool_def, tool_args, interaction_response):
        raise AssertionError("tool must not execute on a consumed checkpoint")

    monkeypatch.setattr(
        LLMLoopRunner, "call_interactive_tool", staticmethod(must_not_execute),
    )

    resume_task = await start_running_task(project, session_id)
    events = await collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=response,
    ))

    assert [e["type"] for e in events] == ["done"]
    _, _, messages = await load_state(project, session_id, parked)
    assert not [m for m in messages if m.role == "tool"]


@pytest.mark.asyncio
async def test_cancel_during_approved_resume_cancels_the_tool(project, monkeypatch):
    """A stop landing while an approved slow tool runs must cancel the tool
    itself instead of waiting out its execution: the resume approval paths
    share the main loop's cancellation wrapper, so the cancelled tool result
    is injected and the turn ends with the cancelled terminal."""
    script_llm(monkeypatch, [script_main_tool_call()])
    session_id, parked = await make_turn(project)
    await collect(QueryLoop(project, session_id, task_id=parked))

    started = asyncio.Event()

    async def slow_call_tool(tool_def, tool_args):
        started.set()
        await asyncio.sleep(60)
        return "must not finish"  # pragma: no cover - cancelled before this

    monkeypatch.setattr(LLMLoopRunner, "call_tool", staticmethod(slow_call_tool))

    cancel_event = asyncio.Event()
    resume_task = await start_running_task(project, session_id)
    events_task = asyncio.create_task(collect(QueryLoop(
        project, session_id, task_id=resume_task,
        interaction_response=await owned_response(project, session_id, {"approved": True}), cancel_event=cancel_event,
    )))
    await asyncio.wait_for(started.wait(), timeout=5)
    cancel_event.set()

    events = await asyncio.wait_for(events_task, timeout=5)

    types = [e["type"] for e in events]
    assert types[-1] == "cancelled"
    assert "done" not in types and "error" not in types
    _, _, messages = await load_state(project, session_id, parked)
    tool_rows = [m for m in messages if m.role == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0].content == "Tool cancelled by user."
