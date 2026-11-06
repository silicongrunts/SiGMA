"""Wiring pins for the project-memory feature's integration seams.

The counter rules live inside the loop kernel, where a test cannot reach
without driving a whole chat turn. The predicates and the turn-end
scheduler are extracted as small named functions precisely so these
contracts stay pinned: which LLM calls deplete the extraction counter, and
that only a completed main-loop turn — final assistant message, no pending
tool calls — hands the due task to the background queue.
"""

import asyncio
from importlib import import_module
from types import SimpleNamespace

import pytest

import_module("app.agents.tools")
from app.services import llm_loop_runner as llr_module
from app.services.llm_loop_runner import (
    LLMLoopRunner,
    LoopContext,
    _depletes_memory_counter,
    _schedule_due_memory_task,
)
from app.services.memory_service import memory_service
from app.services.token_budget import TokenBudgetTracker


def _ctx(**overrides):
    base = {"context_kind": "main", "is_agent_tool": False, "project_id": "p1"}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_only_main_loop_calls_deplete_the_extraction_counter():
    assert _depletes_memory_counter(_ctx()) is True
    # a subagent run (fork/explore/general) is one unit on the calling loop,
    # whatever it does inside
    assert _depletes_memory_counter(_ctx(is_agent_tool=True)) is False
    # annotation turns never feed project extraction
    assert _depletes_memory_counter(_ctx(context_kind="annotation")) is False
    assert _depletes_memory_counter(_ctx(project_id=None)) is False


def test_usage_recording_covers_main_annotation_and_subagents():
    from app.services.llm_loop_runner import _records_memory_usage

    assert _records_memory_usage(_ctx()) is True
    assert _records_memory_usage(_ctx(context_kind="annotation")) is True
    # subagent final results carry the usage-tag contract too
    assert _records_memory_usage(_ctx(is_agent_tool=True)) is True
    assert _records_memory_usage(_ctx(is_agent_tool=True, context_kind="plan")) is True


def test_subagent_final_text_strips_usage_tags():
    """Tags in a subagent result are recorded at loop end; what returns to
    the calling loop (and the tool-activity UI) must carry no markup."""
    from app.services.agent_service import AgentService

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "done — used the setup facts\n"
            '<memory_usage file="a.md">helped setup</memory_usage>',
        },
    ]
    stripped = AgentService._extract_final_text(messages)
    assert "memory_usage" not in stripped
    assert stripped.rstrip() == "done — used the setup facts"


def _capture_scheduling(monkeypatch):
    """Patch the scheduler seam; return the started tasks and counter hits."""
    started = []
    counter_calls = []

    async def fake_due(project_id, session_id=""):
        return "extract"

    async def fake_start(project_id, kind, session_id=""):
        started.append((project_id, kind, session_id))

    async def fake_note(project_id, session_id):
        counter_calls.append((project_id, session_id))

    monkeypatch.setattr(memory_service, "due_task", fake_due)
    monkeypatch.setattr(memory_service, "start_due_task", fake_start)
    monkeypatch.setattr(memory_service, "note_main_loop_llm_call", fake_note)
    return started, counter_calls


async def _drain_scheduler_tasks():
    # fire-and-forget tasks: settle them before asserting on their effects
    while llr_module._memory_scheduler_tasks:
        await asyncio.gather(*list(llr_module._memory_scheduler_tasks))


def _script_turn(monkeypatch, replies, **ctx_kwargs):
    """Script provider replies for one LLMLoopRunner run; return the ctx."""
    calls = {"n": 0}

    async def fake_stream_llm(ctx, messages, delta_queue):
        reply = replies[min(calls["n"], len(replies) - 1)]
        calls["n"] += 1
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm))

    async def persist(messages):
        return None

    return LoopContext(
        project_id="p1",
        session_id="sess-turn",
        persist_messages=persist,
        token_budget_tracker=TokenBudgetTracker(),
        **ctx_kwargs,
    )


@pytest.mark.asyncio
async def test_completed_turn_schedules_due_task(monkeypatch):
    """A turn that ends with a final assistant reply — tool round first,
    then a message with no tool calls — depletes the turn's own session
    window per LLM call and hands the due task to the queue."""
    started, counter_calls = _capture_scheduling(monkeypatch)
    ctx = _script_turn(monkeypatch, [
        ("", "", [{"id": "c1", "name": "sleep", "params": {"duration": 0}}], None),
        ("final answer", "", [], None),
    ])
    events = [e async for e in LLMLoopRunner().run(ctx, [{"role": "system", "content": "s"}])]
    await _drain_scheduler_tasks()

    assert events[-1]["type"] == "done"
    assert counter_calls == [("p1", "sess-turn"), ("p1", "sess-turn")]
    assert started == [("p1", "extract", "sess-turn")]


@pytest.mark.asyncio
async def test_errored_turn_never_schedules(monkeypatch):
    """An errored turn produced no final assistant message, so it must not
    start a background memory task (the counters stay put for the next
    turn)."""
    started, counter_calls = _capture_scheduling(monkeypatch)
    ctx = _script_turn(monkeypatch, [RuntimeError("provider down")])
    events = [e async for e in LLMLoopRunner().run(ctx, [{"role": "system", "content": "s"}])]
    await _drain_scheduler_tasks()

    assert events[-1]["type"] == "error"
    assert counter_calls == []
    assert started == []


@pytest.mark.asyncio
async def test_annotation_turn_never_schedules(monkeypatch):
    """Annotation turns complete like main turns but never feed project
    extraction."""
    started, counter_calls = _capture_scheduling(monkeypatch)
    ctx = _script_turn(
        monkeypatch, [("done", "", [], None)], context_kind="annotation",
    )
    events = [e async for e in LLMLoopRunner().run(ctx, [{"role": "system", "content": "s"}])]
    await _drain_scheduler_tasks()

    assert events[-1]["type"] == "done"
    assert counter_calls == []
    assert started == []


@pytest.mark.asyncio
async def test_scheduler_hands_due_task_with_session(project, monkeypatch):
    started, _ = _capture_scheduling(monkeypatch)

    await _schedule_due_memory_task(project, "sess-7")

    assert started == [(project, "extract", "sess-7")]


@pytest.mark.asyncio
async def test_scheduler_failure_never_raises(project, monkeypatch):
    """A scheduling failure after a completed turn must not surface."""
    async def boom(project_id, session_id=""):
        raise RuntimeError("scheduler gone")

    monkeypatch.setattr(memory_service, "due_task", boom)

    await _schedule_due_memory_task(project, "sess-7")  # must not raise
