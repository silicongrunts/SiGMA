import pytest

import asyncio
from types import SimpleNamespace

import app.agents.tools  # noqa: F401 - register tools
import app.services.agent_service as agent_service_module
import app.services.query_loop as query_loop_module
from app.services.agent_service import AgentService
from app.services.llm_loop_runner import LLMLoopRunner, LoopContext, InteractiveToolPause
from app.services.query_loop import QueryLoop
from app.services.token_budget import TokenBudgetTracker, TokenUsage
from app.core.chat_attachments import render_image_refs_tag
from app.core.config import settings, ModelSettings


def test_fork_inherits_only_complete_parent_context():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": "I will delegate.",
            "tool_calls": [
                {
                    "id": "call_agent",
                    "type": "function",
                    "function": {"name": "agent", "arguments": "{}"},
                }
            ],
        },
    ]

    inherited = LLMLoopRunner._messages_before_tool_call(messages, "call_agent")

    assert inherited == messages[:2]


@pytest.mark.asyncio
async def test_agent_tool_context_carries_parent_request_shape():
    class FakeAgentTool:
        is_agent_tool = True

    observed = {}

    async def execute_tool(tool_name, tool_args):
        from app.agents.tools.agent_tool import agent_exec_context

        ctx = agent_exec_context.get()
        observed.update(ctx)
        return "agent result"

    parent_tools = [{"type": "function", "function": {"name": "agent"}}]
    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        model_role="ra",
        response_max_tokens=1234,
        tool_schemas=parent_tools,
        execute_tool=execute_tool,
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
            ctx, "agent", {"agent_type": ""}, "call_agent", messages,
        )
    ]

    assert any(event["type"] == "tool_end" for event in events)
    assert observed["model_role"] == "ra"
    assert observed["response_max_tokens"] == 1234
    assert observed["tool_schemas"] is parent_tools
    assert observed["messages"] == messages[:1]


@pytest.mark.asyncio
async def test_fork_reuses_parent_request_shape_and_injects_task_messages(monkeypatch):
    captured = {}

    async def fake_run_and_forward(ctx, messages, emit_event=None):
        captured["ctx"] = ctx
        captured["messages"] = list(messages)
        messages.append(LLMLoopRunner.msg("assistant", "fork complete"))

    monkeypatch.setattr(
        AgentService, "_run_and_forward", staticmethod(fake_run_and_forward)
    )

    inherited = [
        {"role": "system", "content": "parent system"},
        {"role": "user", "content": "parent request"},
    ]
    parent_tools = [{"type": "function", "function": {"name": "agent"}}]

    result = await AgentService()._fork(
        prompt="Inspect the agent service.",
        project_id="project-1",
        inherited_messages=inherited,
        emit_event=None,
        cancel_event=None,
        model_role="ra",
        response_max_tokens=777,
        tool_schemas=parent_tools,
    )

    assert result == "fork complete"
    assert captured["messages"][:2] == inherited
    assert captured["messages"][2]["role"] == "user"
    assert "fork mode" in captured["messages"][2]["content"]
    assert "agent" in captured["messages"][2]["content"]
    assert captured["messages"][3]["role"] == "user"
    assert "Inspect the agent service." in captured["messages"][3]["content"]
    assert captured["ctx"].model_role == "ra"
    assert captured["ctx"].response_max_tokens == 777
    assert captured["ctx"].tool_schemas is parent_tools
    assert captured["ctx"].allowed_tools is None
    assert "agent" in captured["ctx"].forbidden_tools
    assert "task_create" in captured["ctx"].forbidden_tools
    assert captured["ctx"].forbidden_tool_context == "fork"


@pytest.mark.asyncio
async def test_fork_forbidden_tool_error_is_fed_back_to_llm(monkeypatch):
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                "",
                "",
                [{"id": "call_agent", "name": "agent", "params": {}}],
                {"prompt_tokens": 10, "completion_tokens": 1},
            )
        return (
            "completed without nesting",
            "",
            [],
            {"prompt_tokens": 10, "completion_tokens": 2},
        )

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    ctx = LoopContext(
        project_id="project-1",
        tool_schemas=[],
        forbidden_tools=frozenset({"agent"}),
        forbidden_tool_context="fork",
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    assert calls == 2
    assert any(event["type"] == "tool_end" for event in events)
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert tool_messages
    assert "cannot be used in fork mode" in tool_messages[-1]["content"]
    assert messages[-1]["content"] == "completed without nesting"


@pytest.mark.asyncio
async def test_fork_does_not_return_inherited_assistant_when_no_final(monkeypatch):
    async def fake_run_and_forward(ctx, messages, emit_event=None):
        return None

    monkeypatch.setattr(
        AgentService, "_run_and_forward", staticmethod(fake_run_and_forward)
    )

    result = await AgentService()._fork(
        prompt="Do the fork task.",
        project_id="project-1",
        inherited_messages=[
            {"role": "system", "content": "system"},
            {"role": "assistant", "content": "old answer"},
        ],
        emit_event=None,
        cancel_event=None,
    )

    assert result == "Error: Fork agent produced no final assistant response."


@pytest.mark.asyncio
async def test_fork_extracts_final_response_after_compaction_replaces_context(monkeypatch):
    async def fake_run_and_forward(ctx, messages, emit_event=None):
        messages[:] = [
            {"role": "system", "content": "system"},
            {"role": "system", "content": "compacted summary"},
            LLMLoopRunner.msg("assistant", "final after compaction"),
        ]

    monkeypatch.setattr(
        AgentService, "_run_and_forward", staticmethod(fake_run_and_forward)
    )

    inherited = [
        {"role": "system", "content": "system"},
        *[
            {"role": "assistant", "content": f"old answer {idx}"}
            for idx in range(12)
        ],
    ]

    result = await AgentService()._fork(
        prompt="Do the fork task.",
        project_id="project-1",
        inherited_messages=inherited,
        emit_event=None,
        cancel_event=None,
    )

    assert result == "final after compaction"


def test_token_usage_total_does_not_double_count_cached_tokens():
    usage = TokenUsage(input=100, output=20, cached=80)

    assert usage.total == 120


@pytest.mark.asyncio
async def test_interactive_pause_persists_and_emits_usage(monkeypatch):
    async def fake_stream_llm(ctx, messages, delta_queue):
        return (
            "",
            "",
            [{
                "id": "call_question",
                "name": "ask_user_question",
                "params": {
                    "questions": [{
                        "question": "Continue?",
                        "type": "single",
                        "options": [
                            {"label": "Yes", "description": "Proceed"},
                            {"label": "No", "description": "Stop"},
                        ],
                    }],
                },
            }],
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        )

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    persisted = []

    async def persist_messages(messages):
        persisted.append(list(messages))

    paused = []

    async def on_pause(**kwargs):
        paused.append(kwargs)

    tracker = TokenBudgetTracker()
    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        task_id="task-1",
        persist_messages=persist_messages,
        on_interactive_pause=on_pause,
        token_budget_tracker=tracker,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    assert paused
    assert events[-2]["type"] == "awaiting_input"
    assert events[-1] == {
        "type": "done",
        "data": {"usage": {"input": 100, "output": 20, "cached": 40}},
    }
    assert persisted[-1][-1]["_input_tokens"] == 100
    assert persisted[-1][-1]["_completion_tokens"] == 20
    assert persisted[-1][-1]["_cached_tokens"] == 40


@pytest.mark.asyncio
async def test_direct_interactive_pause_calls_real_queryloop_callback(monkeypatch):
    """Regression: the runner must call on_interactive_pause with only the
    kwargs the real QueryLoop._on_interactive_pause accepts. This wires the
    REAL callback (not a permissive **kwargs stub) so any signature mismatch
    between the runner and QueryLoop would resurface as a test failure."""
    async def fake_stream_llm(ctx, messages, delta_queue):
        return (
            "",
            "",
            [{
                "id": "call_q",
                "name": "ask_user_question",
                "params": {
                    "questions": [{
                        "question": "Which DB?",
                        "type": "single",
                        "options": [
                            {"label": "Postgres", "description": "relational"},
                            {"label": "Redis", "description": "kv store"},
                        ],
                    }],
                },
            }],
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        )

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    # Neutralize the DB write inside the real callback
    import app.services.query_loop as query_loop_module

    class _NoopTaskState:
        async def mark_awaiting_input(self, *args, **kwargs):
            return None

    class _NoopUow:
        def __init__(self, *args, **kwargs):
            self.task_state = _NoopTaskState()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(query_loop_module, "UnitOfWork", _NoopUow)

    loop = QueryLoop(
        project_id="project-1", session_id="session-1", task_id="task-1",
    )

    async def persist_messages(messages):
        pass

    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        task_id="task-1",
        persist_messages=persist_messages,
        on_interactive_pause=loop._on_interactive_pause,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    # The real strict-signature callback ran without TypeError, and the loop
    # paused cleanly with an awaiting_input event.
    assert any(e["type"] == "awaiting_input" for e in events)


@pytest.mark.asyncio
async def test_interactive_validation_error_fed_back_to_llm(monkeypatch):
    """An invalid ask_user_question payload (empty option label) must be fed
    back to the LLM as a tool result and re-tried, NOT surfaced as an
    awaiting_input modal."""
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                "",
                "",
                [{
                    "id": "call_q",
                    "name": "ask_user_question",
                    "params": {
                        "questions": [{
                            "question": "Pick one",
                            "type": "single",
                            "options": [
                                {"label": "", "description": "empty label"},
                                {"label": "B", "description": "ok"},
                            ],
                        }],
                    },
                }],
                {"prompt_tokens": 10, "completion_tokens": 2,
                 "prompt_tokens_details": {"cached_tokens": 0}},
            )
        return ("final", "", [], {"prompt_tokens": 5, "completion_tokens": 1,
                                  "prompt_tokens_details": {"cached_tokens": 0}})

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    async def persist_messages(messages):
        pass

    ctx = LoopContext(
        project_id="project-1", session_id="session-1",
        persist_messages=persist_messages,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    # The LLM was re-prompted after the error (retry), and never paused.
    assert calls == 2
    assert not any(e["type"] == "awaiting_input" for e in events)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs and "empty 'label'" in tool_msgs[-1]["content"]


@pytest.mark.asyncio
async def test_interactive_missing_questions_arg_does_not_crash(monkeypatch):
    """Regression for Bug A: LLM omits the required 'questions' arg. The
    call failure must be fed back as a tool error and retried, not crash the
    whole turn with a propagating TypeError."""
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                "",
                "",
                [{"id": "call_q", "name": "ask_user_question", "params": {}}],
                {"prompt_tokens": 10, "completion_tokens": 2,
                 "prompt_tokens_details": {"cached_tokens": 0}},
            )
        return ("final", "", [], {"prompt_tokens": 5, "completion_tokens": 1,
                                  "prompt_tokens_details": {"cached_tokens": 0}})

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    async def persist_messages(messages):
        pass

    ctx = LoopContext(
        project_id="project-1", session_id="session-1",
        persist_messages=persist_messages,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    assert calls == 2
    assert not any(e["type"] == "awaiting_input" for e in events)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs and "questions" in tool_msgs[-1]["content"]


@pytest.mark.asyncio
async def test_regular_tool_exception_is_fed_back_to_llm(monkeypatch):
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                "",
                "",
                [{"id": "call_bad", "name": "task_create", "params": {}}],
                {"prompt_tokens": 10, "completion_tokens": 2},
            )
        return (
            "recovered",
            "",
            [],
            {"prompt_tokens": 5, "completion_tokens": 1},
        )

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    async def execute_tool(tool_name, tool_args):
        raise TypeError("missing required argument: subject")

    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        execute_tool=execute_tool,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    assert calls == 2
    assert not any(event["type"] == "error" for event in events)
    assert any(event["type"] == "done" for event in events)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "missing required argument: subject" in tool_msgs[0]["content"]
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "recovered"


@pytest.mark.asyncio
async def test_regular_tool_exceptions_stop_after_three_consecutive_errors(monkeypatch):
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        return (
            "",
            "",
            [{"id": f"call_bad_{calls}", "name": "task_create", "params": {}}],
            {"prompt_tokens": 10, "completion_tokens": 2},
        )

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    async def execute_tool(tool_name, tool_args):
        raise TypeError("missing required argument: subject")

    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        execute_tool=execute_tool,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    assert calls == 3
    error_events = [event for event in events if event["type"] == "error"]
    assert len(error_events) == 1
    assert "invalid tool calls 3 consecutive times" in error_events[0]["data"]["error"]
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert all("missing required argument: subject" in m["content"] for m in tool_msgs)


@pytest.mark.asyncio
async def test_regular_tool_exception_does_not_skip_sibling_tool_calls(monkeypatch):
    calls = 0

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                "",
                "",
                [
                    {"id": "call_bad", "name": "bad_tool", "params": {}},
                    {"id": "call_good", "name": "good_tool", "params": {}},
                ],
                {"prompt_tokens": 10, "completion_tokens": 2},
            )
        tool_call_ids = [
            m.get("tool_call_id")
            for m in messages
            if m.get("role") == "tool"
        ]
        assert tool_call_ids == ["call_bad", "call_good"]
        return ("recovered", "", [], {"prompt_tokens": 5, "completion_tokens": 1})

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    async def execute_tool(tool_name, tool_args):
        if tool_name == "bad_tool":
            raise RuntimeError("bad tool failed")
        return "good tool result"

    ctx = LoopContext(
        project_id="project-1",
        session_id="session-1",
        execute_tool=execute_tool,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    assert calls == 2
    assert not any(event["type"] == "error" for event in events)
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_bad", "call_good"]
    assert "bad tool failed" in tool_msgs[0]["content"]
    assert tool_msgs[1]["content"] == "good tool result"


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
async def test_subagent_pause_done_event_includes_shared_usage(monkeypatch):
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

    assert events[-1] == {
        "type": "done",
        "data": {"usage": {"input": 300, "output": 60, "cached": 128}},
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
    class FakeAgentTool:
        is_agent_tool = True

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
    class FakeAgentTool:
        is_agent_tool = True

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


@pytest.mark.asyncio
async def test_build_messages_context_baseline_uses_latest_assistant_input_only(monkeypatch, tmp_path):
    history = [
        SimpleNamespace(
            role="user", content="question", tool_calls=None, tool_call_id=None,
            reasoning_content=None, input_tokens=0,
        ),
        SimpleNamespace(
            role="assistant", content="call agent", tool_calls=None, tool_call_id=None,
            reasoning_content=None, input_tokens=28_000,
        ),
        SimpleNamespace(
            role="tool", content="agent result", tool_calls=None, tool_call_id="call_agent",
            reasoning_content=None, input_tokens=450_000,
        ),
    ]

    class FakeMessages:
        async def get_messages_for_llm(self, session_id):
            return history

    class FakeConfig:
        async def get(self, key, default=""):
            return default

    class FakeUnitOfWork:
        def __init__(self, project_id):
            self.messages = FakeMessages()
            self.config = FakeConfig()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(query_loop_module, "UnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr(
        query_loop_module.session_temp_service,
        "session_dir_for_prompt",
        lambda project_id, session_id: str(tmp_path / ".SiGMA" / "sessions" / session_id),
    )
    monkeypatch.setattr(
        query_loop_module.prompt_service,
        "build_system_prompt",
        lambda **kwargs: "system",
    )
    monkeypatch.setattr(QueryLoop, "_describe_agents", lambda self: "")

    loop = QueryLoop(project_id="project-1", session_id="session-1")
    messages = await loop._build_messages()

    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool"]
    assert loop._persisted_real_input_tokens == 28_000
    assert loop._persisted_real_count_at_index == 2


@pytest.mark.asyncio
async def test_build_messages_rehydrates_tool_image_refs_for_multimodal_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(settings.models, "supervisor", ModelSettings(model="supervisor-model"))
    monkeypatch.setattr(settings.models, "vision", ModelSettings(reuse="supervisor"))

    image_tag = render_image_refs_tag([{
        "path": "/tmp/read-image.png",
        "mime_type": "image/png",
        "source": "read",
        "text": "Image file: /tmp/read-image.png (10x10)",
    }])
    history = [
        SimpleNamespace(
            role="tool",
            content=f"Image file: /tmp/read-image.png (10x10){image_tag}",
            tool_calls=None,
            tool_call_id="call_read",
            reasoning_content=None,
            input_tokens=0,
        ),
    ]

    class FakeMessages:
        async def get_messages_for_llm(self, session_id):
            return history

    class FakeConfig:
        async def get(self, key, default=""):
            return default

    class FakeUnitOfWork:
        def __init__(self, project_id):
            self.messages = FakeMessages()
            self.config = FakeConfig()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_read_image_path_base64(project_id, path):
        assert path == "/tmp/read-image.png"
        return "aW1hZ2U=", "image/png"

    monkeypatch.setattr(query_loop_module, "UnitOfWork", FakeUnitOfWork)
    monkeypatch.setattr(
        query_loop_module.session_temp_service,
        "session_dir_for_prompt",
        lambda project_id, session_id: str(tmp_path / ".SiGMA" / "sessions" / session_id),
    )
    monkeypatch.setattr(query_loop_module, "read_image_path_base64", fake_read_image_path_base64)
    monkeypatch.setattr(
        query_loop_module.prompt_service,
        "build_system_prompt",
        lambda **kwargs: "system",
    )
    monkeypatch.setattr(QueryLoop, "_describe_agents", lambda self: "")

    loop = QueryLoop(project_id="project-1", session_id="session-1")
    messages = await loop._build_messages()

    assert [m["role"] for m in messages] == ["system", "tool", "user"]
    assert "<image_refs>" not in messages[1]["content"]
    assert messages[2]["_ephemeral"] is True
    assert messages[2]["content"][1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="


@pytest.mark.asyncio
async def test_query_persist_skips_rehydrated_ephemeral_images_without_duplicate_history(monkeypatch):
    class FakeMessages:
        def __init__(self):
            self.created = []

        async def get_messages_for_llm(self, session_id):
            return [
                type("Message", (), {"role": "user"})(),
                type("Message", (), {"role": "assistant"})(),
            ]

        async def stage_create(self, **kwargs):
            self.created.append(kwargs)
            return type("Message", (), {"id": "new-message"})()

    class FakeSessions:
        def __init__(self):
            self.touched = []

        async def stage_touch(self, session_id):
            self.touched.append(session_id)

    fake_messages = FakeMessages()
    fake_sessions = FakeSessions()

    class FakeUnitOfWork:
        @staticmethod
        async def execute_atomic(project_id, operation):
            uow = type("Uow", (), {
                "messages": fake_messages,
                "sessions": fake_sessions,
            })()
            return await operation(uow)

    monkeypatch.setattr(query_loop_module, "UnitOfWork", FakeUnitOfWork)

    loop = QueryLoop(project_id="project-1", session_id="session-1")
    await loop._save_messages([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old user"},
        {"role": "user", "content": [{"type": "text", "text": "old image"}], "_ephemeral": True},
        {"role": "assistant", "content": "old assistant"},
        {"role": "assistant", "content": "new assistant"},
    ])

    assert [m["content"] for m in fake_messages.created] == ["new assistant"]
    assert fake_sessions.touched == ["session-1"]


@pytest.mark.asyncio
async def test_query_persist_writes_zero_for_messages_without_real_usage(monkeypatch):
    class FakeMessages:
        def __init__(self):
            self.created = []

        async def get_messages_for_llm(self, session_id):
            return []

        async def create(self, **kwargs):
            self.created.append(kwargs)
            return type("Message", (), {"id": "new-message"})()

        async def stage_create(self, **kwargs):
            return await self.create(**kwargs)

    class FakeSessions:
        async def touch(self, session_id):
            pass

        async def stage_touch(self, session_id):
            await self.touch(session_id)

    fake_messages = FakeMessages()

    class FakeUnitOfWork:
        def __init__(self, project_id):
            self.messages = fake_messages
            self.sessions = FakeSessions()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        async def execute_atomic(project_id, operation):
            return await operation(FakeUnitOfWork(project_id))

    monkeypatch.setattr(query_loop_module, "UnitOfWork", FakeUnitOfWork)

    loop = QueryLoop(project_id="project-1", session_id="session-1")
    await loop._save_messages([
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "fallback text without provider usage"},
    ])

    assert fake_messages.created[0]["role"] == "user"
    assert fake_messages.created[0]["token_count"] == 0
    assert fake_messages.created[0]["input_tokens"] == 0
    assert fake_messages.created[0]["cached_tokens"] == 0
    assert fake_messages.created[1]["role"] == "assistant"
    assert fake_messages.created[1]["token_count"] == 0
    assert fake_messages.created[1]["input_tokens"] == 0
    assert fake_messages.created[1]["cached_tokens"] == 0


@pytest.mark.asyncio
async def test_agent_persist_does_not_update_prior_assistant_when_final_save_has_no_new_messages(monkeypatch):
    class FakeMessages:
        def __init__(self):
            self.created = []

        async def get_messages_for_llm(self, session_id):
            return [
                type("Message", (), {"role": "user"})(),
                type("Message", (), {"role": "assistant"})(),
                type("Message", (), {"role": "tool"})(),
            ]

        async def create(self, **kwargs):
            self.created.append(kwargs)
            return type("Message", (), {"id": "new-message"})()

        async def stage_create(self, **kwargs):
            return await self.create(**kwargs)

    class FakeSessions:
        def __init__(self):
            self.touched = []

        async def touch(self, session_id):
            self.touched.append(session_id)

        async def stage_touch(self, session_id):
            await self.touch(session_id)

    fake_messages = FakeMessages()
    fake_sessions = FakeSessions()

    class FakeUnitOfWork:
        def __init__(self, project_id):
            self.messages = fake_messages
            self.sessions = fake_sessions

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        async def execute_atomic(project_id, operation):
            return await operation(FakeUnitOfWork(project_id))

    monkeypatch.setattr(agent_service_module, "UnitOfWork", FakeUnitOfWork)

    await AgentService()._persist_agent_messages(
        "project-1",
        "agent-session",
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "prompt"},
            {"role": "assistant", "content": "tool call"},
            {"role": "tool", "content": "tool result"},
        ],
    )

    assert fake_messages.created == []
    assert fake_sessions.touched == ["agent-session"]


@pytest.mark.asyncio
async def test_agent_persist_writes_per_message_usage(monkeypatch):
    class FakeMessages:
        def __init__(self):
            self.created = []

        async def get_messages_for_llm(self, session_id):
            return []

        async def create(self, **kwargs):
            self.created.append(kwargs)
            return type("Message", (), {"id": "new-message"})()

        async def stage_create(self, **kwargs):
            return await self.create(**kwargs)

    class FakeSessions:
        async def touch(self, session_id):
            pass

        async def stage_touch(self, session_id):
            await self.touch(session_id)

    fake_messages = FakeMessages()

    class FakeUnitOfWork:
        def __init__(self, project_id):
            self.messages = fake_messages
            self.sessions = FakeSessions()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        async def execute_atomic(project_id, operation):
            return await operation(FakeUnitOfWork(project_id))

    monkeypatch.setattr(agent_service_module, "UnitOfWork", FakeUnitOfWork)

    await AgentService()._persist_agent_messages(
        "project-1",
        "agent-session",
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "prompt"},
            {
                "role": "assistant",
                "content": "tool call",
                "_input_tokens": 100,
                "_completion_tokens": 20,
                "_cached_tokens": 40,
            },
            {
                "role": "tool",
                "content": "agent result",
                "tool_call_id": "call_agent",
                "_input_tokens": 300,
                "_completion_tokens": 60,
                "_cached_tokens": 120,
            },
        ],
    )

    assert fake_messages.created[0]["role"] == "user"
    assert fake_messages.created[0]["input_tokens"] == 0
    assert fake_messages.created[0]["token_count"] == 0
    assert fake_messages.created[0]["cached_tokens"] == 0
    assert fake_messages.created[1]["role"] == "assistant"
    assert fake_messages.created[1]["input_tokens"] == 100
    assert fake_messages.created[1]["token_count"] == 20
    assert fake_messages.created[1]["cached_tokens"] == 40
    assert fake_messages.created[2]["role"] == "tool"
    assert fake_messages.created[2]["input_tokens"] == 300
    assert fake_messages.created[2]["token_count"] == 60
    assert fake_messages.created[2]["cached_tokens"] == 120


@pytest.mark.asyncio
async def test_agent_persist_does_not_update_old_assistant_without_persisted_marker(monkeypatch):
    persisted_calls = []

    class FakeMessages:
        async def get_messages_for_llm(self, session_id):
            return [type("Message", (), {})()]

        async def stage_create(self, **kwargs):
            persisted_calls.append(kwargs)

    class FakeSessions:
        async def touch(self, session_id):
            pass

        async def stage_touch(self, session_id):
            await self.touch(session_id)

    class FakeUnitOfWork:
        def __init__(self, project_id):
            self.messages = FakeMessages()
            self.sessions = FakeSessions()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        async def execute_atomic(project_id, operation):
            return await operation(FakeUnitOfWork(project_id))

    monkeypatch.setattr(agent_service_module, "UnitOfWork", FakeUnitOfWork)

    await AgentService()._persist_agent_messages(
        "project-1",
        "agent-session",
        [{"role": "system", "content": "system"}],
    )

    # Only a system message was passed; nothing should be persisted.
    assert persisted_calls == []


# ---------------------------------------------------------------------------
# Regression: requires_session_id tools in session-less sub-loops
# ---------------------------------------------------------------------------
#
# Bug: AnnotationLoop, _spawn_explore, and _fork built LoopContext with
# ``session_id=None`` because they have no session row. The loop runner only
# injects session_id when ``ctx.session_id`` is truthy, so read/notebook_read
# (which declare session_id as a required positional param) raised
# "missing 1 required positional argument: 'session_id'". The fix passes a
# stable per-scope namespace key ("annotation:<id>" / "agent:explore:<uuid>"
# / "agent:fork:<uuid>") so injection succeeds and read-state stays isolated.
#
# These tests drive the REAL tool-injection + execution path: a fake LLM emits
# a read tool_call, run() injects project_id/session_id, and the real read tool
# executes against a tmp_path sandbox. The assertions pin both directions — a
# truthy scope key makes read succeed, and the legacy None still surfaces the
# original error so future regressions are not silently masked.


@pytest.mark.asyncio
async def test_read_succeeds_with_annotation_scope_key(monkeypatch, tmp_path):
    """The annotation namespace key must let read run via the real injection path."""
    from app.services.file_service import file_service

    (tmp_path / "doc.md").write_text("hello annotation")
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)

    calls = {"n": 0}

    async def fake_stream_llm(ctx, messages, delta_queue):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                "",
                "",
                [{"id": "call_read", "name": "read", "params": {"file_path": "doc.md"}}],
                {"prompt_tokens": 5, "completion_tokens": 1},
            )
        # Second turn: the read result is in context, so emit final text and stop.
        return ("final", "", [], {"prompt_tokens": 5, "completion_tokens": 1})

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    ctx = LoopContext(
        project_id="project-1",
        session_id=f"annotation:ann-1",
        model_role="supervisor",
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    assert "missing 1 required positional argument" not in tool_msgs[-1]["content"]
    assert "hello annotation" in tool_msgs[-1]["content"]
    assert any(e["type"] == "done" for e in events)


@pytest.mark.asyncio
async def test_read_succeeds_with_agent_scope_key(monkeypatch, tmp_path):
    """explore/fork one-shot scope keys must also let read run."""
    from app.services.file_service import file_service

    (tmp_path / "doc.md").write_text("hello agent")
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)

    calls = {"n": 0}

    async def fake_stream_llm(ctx, messages, delta_queue):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                "",
                "",
                [{"id": "call_read", "name": "read", "params": {"file_path": "doc.md"}}],
                {"prompt_tokens": 5, "completion_tokens": 1},
            )
        return ("final", "", [], {"prompt_tokens": 5, "completion_tokens": 1})

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    ctx = LoopContext(
        project_id="project-1",
        session_id=f"agent:explore:{'0' * 32}",
        model_role="ra",
    )
    messages = [{"role": "system", "content": "system"}]

    async for _ in LLMLoopRunner().run(ctx, messages):
        pass

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    assert "missing 1 required positional argument" not in tool_msgs[-1]["content"]
    assert "hello agent" in tool_msgs[-1]["content"]


@pytest.mark.asyncio
async def test_read_still_fails_when_session_id_is_none(monkeypatch, tmp_path):
    """Guard against silent regression: a None session_id must surface the
    original missing-argument error (it must NOT be masked), so a future
    session-less sub-loop that forgets to pass a scope key fails loudly."""
    from app.services.file_service import file_service

    (tmp_path / "doc.md").write_text("hello")
    monkeypatch.setattr(file_service, "get_project_path", lambda pid: tmp_path)

    async def fake_stream_llm(ctx, messages, delta_queue):
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                "",
                "",
                [{"id": "call_read", "name": "read", "params": {"file_path": "doc.md"}}],
                {"prompt_tokens": 5, "completion_tokens": 1},
            )
        return ("final", "", [], {"prompt_tokens": 5, "completion_tokens": 1})

    monkeypatch.setattr(
        LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm)
    )

    calls = 0
    ctx = LoopContext(project_id="project-1", session_id=None, model_role="supervisor")
    messages = [{"role": "system", "content": "system"}]

    async for _ in LLMLoopRunner().run(ctx, messages):
        pass

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    assert "missing 1 required positional argument: 'session_id'" in tool_msgs[-1]["content"]
