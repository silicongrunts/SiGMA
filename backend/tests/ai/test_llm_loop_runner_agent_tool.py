"""LLM loop runner: agent tool, fork sub-loops, and tool execution.

Covers the agent tool boundary (parent request-shape inheritance, fork mode
forbidden tools, final-response extraction), interactive pauses fed back to
the LLM, regular tool error feedback and the three-consecutive-error stop,
session-less scope keys for requires_session_id tools, the tool_end payload
contract, and the pending agent usage carry.
"""

from importlib import import_module

import pytest

import_module("app.agents.tools")
from app.services.agent_service import AgentService
from app.services.llm_loop_runner import LLMLoopRunner, LoopContext
from app.services.query_loop import QueryLoop
from app.services.token_budget import TokenBudgetTracker, TokenUsage


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
async def test_interactive_pause_persists_and_ends_without_done(monkeypatch):
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
        on_pause=on_pause,
        token_budget_tracker=tracker,
    )
    messages = [{"role": "system", "content": "system"}]

    events = [event async for event in LLMLoopRunner().run(ctx, messages)]

    assert paused
    # Parking is not a loop terminal event: the loop emits awaiting_input and
    # returns; the task runner synthesizes the done frame for the parked task.
    assert events[-1]["type"] == "awaiting_input"
    assert not any(e["type"] == "done" for e in events)
    assert persisted[-1][-1]["_input_tokens"] == 100
    assert persisted[-1][-1]["_completion_tokens"] == 20
    assert persisted[-1][-1]["_cached_tokens"] == 40


@pytest.mark.asyncio
async def test_direct_interactive_pause_calls_real_queryloop_callback(monkeypatch):
    """Regression: the runner must call on_pause with only the
    kwargs the real QueryLoop._on_pause accepts. This wires the
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
        on_pause=loop._on_pause,
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
        session_id="annotation:ann-1",
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


def test_tool_end_payload_carries_file_edit_only_for_real_edits():
    """The tool_end payload feeds the chat timeline's file-edit card, and the
    permission-resume paths in query_loop reuse it. Pin the contract: a
    successful edit/write carries file_edit; a denial (args=None, the call
    never executed) and a failed call must not."""
    args = {"file_path": "a.py", "old_string": "x", "new_string": "y"}

    payload = LLMLoopRunner.tool_end_payload(
        "edit", args, "File edited: a.py (1 replacement(s))", "call_1",
    )
    assert payload["tool"] == "edit"
    assert payload["tool_call_id"] == "call_1"
    assert payload["file_edit"]["path"] == "a.py"

    # Denied permission: query_loop passes None so no card implies an edit
    # that never ran.
    denied = LLMLoopRunner.tool_end_payload(
        "edit", None, "User denied permission to modify file: a.py", "call_1",
    )
    assert "file_edit" not in denied

    failed = LLMLoopRunner.tool_end_payload(
        "edit", args, "Tool 'edit' error: disk full", "call_1",
    )
    assert "file_edit" not in failed


def test_pending_agent_usage_carry_is_the_delta_since_call_start():
    """The carry stamped onto a propagating pause is the subagent spend
    accrued since the enclosing agent call began — the part that lives
    only in the tracker while the checkpoint parks the turn."""
    tracker = TokenBudgetTracker()
    tracker.add_llm_usage({
        "prompt_tokens": 500, "completion_tokens": 40,
        "prompt_tokens_details": {"cached_tokens": 120},
    })
    ctx = LoopContext(project_id="project-1", token_budget_tracker=tracker)

    carry = LLMLoopRunner._pending_agent_usage_carry(
        ctx, {"input": 100, "output": 10, "cached": 20},
    )

    assert carry == {"input": 400, "output": 30, "cached": 100}


def test_pending_agent_usage_carry_clamps_negative_deltas():
    tracker = TokenBudgetTracker()
    tracker.add_llm_usage({"prompt_tokens": 10, "completion_tokens": 0})
    ctx = LoopContext(project_id="project-1", token_budget_tracker=tracker)

    carry = LLMLoopRunner._pending_agent_usage_carry(
        ctx, {"input": 999, "output": 999, "cached": 999},
    )

    assert carry == {"input": 0, "output": 0, "cached": 0}
