"""LLM loop runner usage accounting.

Cancel, provider error, progressive per-call reporting, and subagent
subtree-usage bookkeeping: every exit path must persist and report the
token spend that actually happened, with cached tokens counted once.
"""

import asyncio
from importlib import import_module

import pytest

import_module("app.agents.tools")
from app.services.llm_loop_runner import (
    InteractiveToolPause,
    LLMLoopRunner,
    LoopContext,
)
from app.services.query_loop import QueryLoop
from app.services.token_budget import TokenBudgetTracker


@pytest.mark.asyncio
async def test_tool_round_persisted_output_is_marked_for_final_dedup(monkeypatch):
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                "",
                "",
                [{"id": "call_sleep", "name": "sleep", "params": {"duration": 0}}],
                {
                    "prompt_tokens": 13922,
                    "completion_tokens": 104,
                    "prompt_tokens_details": {"cached_tokens": 13696},
                },
            )
        return (
            "final",
            "",
            [],
            {
                "prompt_tokens": 16070,
                "completion_tokens": 355,
                "prompt_tokens_details": {"cached_tokens": 13952},
            },
        )

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    persisted_messages = []

    async def persist_messages(messages):
        persisted_messages.append(list(messages))

    tracker = TokenBudgetTracker()
    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        persist_messages=persist_messages,
        token_budget_tracker=tracker,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    assert calls == 2
    assert persisted_messages
    assert persisted_messages[-1][-1]["_input_tokens"] == 16070
    assert persisted_messages[-1][-1]["_completion_tokens"] == 355
    assert persisted_messages[-1][-1]["_cached_tokens"] == 13952
    assert events[-1] == {
        "type": "done",
        "data": {"usage": {"input": 29992, "output": 459, "cached": 27648}},
    }


@pytest.mark.asyncio
async def test_subagent_pause_emits_awaiting_input_without_done(monkeypatch):
    """A subagent pause parks the task: the loop emits the agent_event and
    awaiting_input frames only — no done frame, which the task runner
    synthesizes from the parked awaiting_input status."""
    tracker = TokenBudgetTracker()
    tracker.add_llm_usage({
        "prompt_tokens": 300,
        "completion_tokens": 60,
        "prompt_tokens_details": {"cached_tokens": 128},
    })
    loop = QueryLoop(
        project_id="project-1",
        session_id="session-1",
        task_id="task-1",
        token_budget_tracker=tracker,
    )

    async def fake_save_checkpoint(pause):
        return None

    monkeypatch.setattr(loop, "_save_subagent_checkpoint", fake_save_checkpoint)

    pause = InteractiveToolPause(
        tool_name="ask_user_question",
        tool_args={},
        tool_call_id="inner-call",
        interaction_data={"interaction_type": "ask_user_question"},
        agent_session_id="agent-session",
        agent_type="general",
        parent_tool_call_id="parent-call",
    )

    events = [event async for event in loop._emit_subagent_pause(pause)]

    assert [e["type"] for e in events] == ["agent_event", "awaiting_input"]
    # The dialog payload carries the owning task id as the frontend's
    # unique interaction id.
    assert events[-1]["data"] == {
        "interaction_type": "ask_user_question",
        "task_id": "task-1",
    }


# ---------------------------------------------------------------------------
# Cancel and error exit paths — usage persistence and reporting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_between_turns_persists_usage(monkeypatch):
    """User cancels between LLM turns → usage persisted and included in cancelled event."""
    calls = 0
    cancel_event = asyncio.Event()

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        # First call: return a tool call so the loop continues
        if calls == 1:
            return (
                "",
                "",
                [{"id": "call_sleep", "name": "sleep", "params": {"duration": 0}}],
                {
                    "prompt_tokens": 500,
                    "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 200},
                },
            )
        # Second call: never reached (cancel detected before LLM call)
        return ("text", "", [], None)

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    persisted = []

    async def persist_messages(messages):
        persisted.append(list(messages))
        # Cancel after first tool round is persisted
        if not cancel_event.is_set():
            cancel_event.set()

    tracker = TokenBudgetTracker()
    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        cancel_event=cancel_event,
        persist_messages=persist_messages,
        token_budget_tracker=tracker,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    cancel_events = [e for e in events if e["type"] == "cancelled"]
    assert len(cancel_events) == 1
    # Only first call's usage is counted (cancel before second LLM call)
    assert cancel_events[0]["data"]["usage"] == {"input": 500, "output": 50, "cached": 200}
    assert persisted


@pytest.mark.asyncio
async def test_cancel_during_stream_persists_usage(monkeypatch):
    """User cancels while LLM is streaming → usage persisted for prior turns."""
    calls = 0
    cancel_event = asyncio.Event()

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            # First call: tool call to keep loop going
            return (
                "",
                "",
                [{"id": "call_sleep", "name": "sleep", "params": {"duration": 0}}],
                {
                    "prompt_tokens": 300,
                    "completion_tokens": 30,
                    "prompt_tokens_details": {"cached_tokens": 100},
                },
            )
        # Second call: cancel while streaming
        cancel_event.set()
        await delta_queue.put(("delta", "partial"))
        await delta_queue.put(("__result__", ("", "", [], None)))

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    persisted = []

    async def persist_messages(messages):
        persisted.append(list(messages))

    tracker = TokenBudgetTracker()
    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        cancel_event=cancel_event,
        persist_messages=persist_messages,
        token_budget_tracker=tracker,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    cancel_events = [e for e in events if e["type"] == "cancelled"]
    assert len(cancel_events) == 1
    assert cancel_events[0]["data"]["usage"] == {"input": 300, "output": 30, "cached": 100}
    assert persisted


@pytest.mark.asyncio
async def test_llm_error_persists_usage(monkeypatch):
    """LLM provider error → usage for prior turns persisted, error event with usage."""
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            # First call: tool call to keep loop going
            return (
                "",
                "",
                [{"id": "call_sleep", "name": "sleep", "params": {"duration": 0}}],
                {
                    "prompt_tokens": 400,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 150},
                },
            )
        # Second call: error
        await delta_queue.put(("__error__", ConnectionError("provider dropped")))

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    persisted = []

    async def persist_messages(messages):
        persisted.append(list(messages))

    tracker = TokenBudgetTracker()
    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        persist_messages=persist_messages,
        token_budget_tracker=tracker,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    assert "provider dropped" in error_events[0]["data"]["error"]
    assert error_events[0]["data"]["usage"] == {"input": 400, "output": 40, "cached": 150}

    assert persisted
    usage_message = next(
        msg for snapshot in persisted for msg in snapshot
        if msg.get("_input_tokens") == 400
    )
    assert usage_message["_completion_tokens"] == 40
    assert usage_message["_cached_tokens"] == 150
    final_message = persisted[-1][-1]
    assert final_message["role"] == "assistant"
    assert "provider dropped" in final_message["content"]

    done_events = [e for e in events if e["type"] == "done"]
    assert done_events == []


@pytest.mark.asyncio
async def test_cancel_no_usage_when_no_llm_calls(monkeypatch):
    """Cancel before any LLM call → no usage, cancelled event has no usage key."""

    async def fake_stream_llm(ctx, messages, delta_queue):
        return ("text", "", [], None)

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    cancel_event = asyncio.Event()
    cancel_event.set()  # Already cancelled

    persisted = []

    async def persist_messages(messages):
        persisted.append(list(messages))

    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        cancel_event=cancel_event,
        persist_messages=persist_messages,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    cancel_events = [e for e in events if e["type"] == "cancelled"]
    assert len(cancel_events) == 1
    assert "usage" not in cancel_events[0]["data"]
    # Persist should be called without adding usage metadata.
    assert persisted
    assert all("_input_tokens" not in msg for msg in persisted[-1])


@pytest.mark.asyncio
async def test_progressive_usage_emitted_after_each_llm_call(monkeypatch):
    """turn_usage event is emitted after each LLM call in the tool loop."""
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                "",
                "",
                [{"id": "call_sleep", "name": "sleep", "params": {"duration": 0}}],
                {
                    "prompt_tokens": 500,
                    "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 200},
                },
            )
        return (
            "final text",
            "",
            [],
            {
                "prompt_tokens": 200,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 100},
            },
        )

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    async def persist_messages(messages):
        pass

    tracker = TokenBudgetTracker()
    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        persist_messages=persist_messages,
        token_budget_tracker=tracker,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    usage_events = [e for e in events if e["type"] == "turn_usage"]
    assert len(usage_events) == 2
    # First call: 500/50/200
    assert usage_events[0]["data"]["usage"] == {"input": 500, "output": 50, "cached": 200}
    # Second call: cumulative 700/70/300
    assert usage_events[1]["data"]["usage"] == {"input": 700, "output": 70, "cached": 300}


@pytest.mark.asyncio
async def test_subagent_progressive_usage_uses_shared_turn_total(monkeypatch):
    async def fake_stream_llm(ctx, messages, delta_queue):
        return (
            "done",
            "",
            [],
            {
                "prompt_tokens": 50,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 25},
            },
        )

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    tracker = TokenBudgetTracker()
    tracker.add_llm_usage({
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 80},
    })
    ctx = LoopContext(
        project_id="project-1",
        session_id="agent-session",
        is_agent_tool=True,
        token_budget_tracker=tracker,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    usage_events = [e for e in events if e["type"] == "turn_usage"]
    assert usage_events == [{
        "type": "turn_usage",
        "data": {"usage": {"input": 150, "output": 15, "cached": 105}},
    }]


@pytest.mark.asyncio
async def test_agent_tool_result_records_subtree_usage_delta():
    tracker = TokenBudgetTracker()
    tracker.add_llm_usage({
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 50},
    })

    async def execute_tool(tool_name, tool_args):
        tracker.add_llm_usage({
            "prompt_tokens": 300,
            "completion_tokens": 60,
            "prompt_tokens_details": {"cached_tokens": 120},
        })
        tracker.add_llm_usage({
            "prompt_tokens": 200,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 80},
        })
        return "agent result"

    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        execute_tool=execute_tool,
        token_budget_tracker=tracker,
    )
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_agent",
                "type": "function",
                "function": {"name": "agent", "arguments": "{}"},
            }],
        },
    ]

    events = [
        event async for event in LLMLoopRunner()._run_agent_tool(
            ctx, "agent", {}, "call_agent", messages,
        )
    ]

    assert any(event["type"] == "tool_end" for event in events)
    tool_message = messages[-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_agent"
    assert tool_message["_input_tokens"] == 500
    assert tool_message["_completion_tokens"] == 100
    assert tool_message["_cached_tokens"] == 200


@pytest.mark.asyncio
async def test_agent_tool_error_returns_tool_result_and_records_subtree_usage_delta():
    tracker = TokenBudgetTracker()

    async def execute_tool(tool_name, tool_args):
        tracker.add_llm_usage({
            "prompt_tokens": 123,
            "completion_tokens": 45,
            "prompt_tokens_details": {"cached_tokens": 67},
        })
        raise RuntimeError("subagent failed")

    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        execute_tool=execute_tool,
        token_budget_tracker=tracker,
    )
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_agent",
                "type": "function",
                "function": {"name": "agent", "arguments": "{}"},
            }],
        },
    ]

    events = [
        event async for event in LLMLoopRunner()._run_agent_tool(
            ctx, "agent", {}, "call_agent", messages,
        )
    ]

    assert any(event["type"] == "tool_end" for event in events)
    assert "subagent failed" in messages[-1]["content"]
    assert messages[-1]["_input_tokens"] == 123
    assert messages[-1]["_completion_tokens"] == 45
    assert messages[-1]["_cached_tokens"] == 67

