"""A paused round's sibling tool calls must execute on resume.

Regression for the dropped-sibling bug: a permission pause mid-round
checkpoints the assistant message of a multi-call batch before its later
calls ran; the resume path executes only the approved call. The runner
entry now completes the pending tail round — every unpaired call of the
tail assistant message runs through the same executor (gates included)
before the next LLM call, so nothing is silently dropped and the
tool_call/result pairing contract holds. Mirrors the production shape:
``bash`` issued first in a 9-call round, gated, approved, and its eight
siblings left dangling until refresh rendered them "interrupted".
"""

import json
from importlib import import_module

import pytest

import_module("app.agents.tools")
from app.services.llm_loop_runner import LLMLoopRunner, LoopContext
from app.services.pauses import PermissionRequestPause

USAGE = {"prompt_tokens": 10, "completion_tokens": 5}


def wire_call(call_id, name, **params):
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(params)},
    }


def paused_round_messages():
    """The exact state a permission resume re-enters the loop with: the
    approved first call answered, its siblings still dangling."""
    return [
        {"role": "user", "content": "run the tool batch"},
        {"role": "assistant", "content": "", "tool_calls": [
            wire_call("call-bash", "bash", command="head -c 16 x"),
            wire_call("call-edit", "edit", file_path="a.txt"),
            wire_call("call-glob", "glob", pattern="many/*.txt"),
        ]},
        {"role": "tool", "content": "ok:bash", "tool_call_id": "call-bash"},
    ]


def make_ctx(recorder):
    """LoopContext whose executor returns ``ok:<tool>``; every stream, tool
    execution, pause, and persist is recorded for assertions."""

    async def execute_tool(name, args):
        recorder["executed"].append(name)
        if name in recorder.get("gate", ()):
            raise PermissionRequestPause(tool=name, tool_name=name)
        return f"ok:{name}"

    async def persist_messages(messages):
        recorder["persists"].append([dict(m) for m in messages])

    async def on_pause(**kwargs):
        recorder["pauses"].append(kwargs["tool_call_id"])

    return LoopContext(
        project_id="proj-test",
        execute_tool=execute_tool,
        persist_messages=persist_messages,
        on_pause=on_pause,
    )


def script_stream(monkeypatch, recorder):
    """One final text-only LLM round; captures the history each call saw."""

    async def fake_stream_llm(ctx, messages, delta_queue):
        recorder["streams"].append([dict(m) for m in messages])
        return ("all done", "", [], USAGE)

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm))


async def collect(run_gen):
    return [event async for event in run_gen]


@pytest.fixture
def recorder():
    return {"executed": [], "pauses": [], "persists": [], "streams": []}


@pytest.mark.asyncio
async def test_resume_executes_pending_siblings_before_llm_call(
    recorder, monkeypatch,
):
    """The dangling edit/glob of the paused round execute on re-entry, the
    answered bash is NOT re-executed, and the LLM sees a fully paired
    history — the exact counterpart of the ea83a9f7 broken session."""
    script_stream(monkeypatch, recorder)
    ctx = make_ctx(recorder)
    messages = paused_round_messages()

    events = await collect(LLMLoopRunner().run(ctx, messages))

    assert events[-1]["type"] == "done"
    assert recorder["executed"] == ["edit", "glob"]

    starts = [
        e["data"]["tool"] for e in events if e["type"] == "tool_start"
    ]
    assert starts == ["edit", "glob"]

    # The continuation persists the executed results before streaming.
    assert recorder["persists"], "pending round was not persisted"
    first_stream_seen = recorder["streams"][0]
    answered = {
        m["tool_call_id"] for m in first_stream_seen if m["role"] == "tool"
    }
    assert answered == {"call-bash", "call-edit", "call-glob"}

    ends = [e for e in events if e["type"] == "tool_end"]
    assert {e["data"]["tool_call_id"] for e in ends} == {
        "call-edit", "call-glob",
    }
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "all done"


@pytest.mark.asyncio
async def test_pause_during_continuation_parks_and_converges(
    recorder, monkeypatch,
):
    """A gated sibling parks the loop again (awaiting_input, no terminal
    frame); after the next resume answers it, the remaining calls execute
    and the turn completes. Each resume consumes at least one call."""
    script_stream(monkeypatch, recorder)
    recorder["gate"] = {"edit"}
    ctx = make_ctx(recorder)
    messages = paused_round_messages()

    events = await collect(LLMLoopRunner().run(ctx, messages))

    assert events[-1]["type"] == "awaiting_input"
    assert "done" not in [e["type"] for e in events]
    assert "error" not in [e["type"] for e in events]
    assert recorder["executed"] == ["edit"]
    assert recorder["pauses"] == ["call-edit"]

    # Resume: the approved edit's result lands (executed by the resume path,
    # outside the runner) and the loop re-enters with the gate lifted.
    recorder["gate"] = set()
    messages.append({
        "role": "tool", "content": "ok:edit", "tool_call_id": "call-edit",
    })

    events = await collect(LLMLoopRunner().run(ctx, messages))

    assert events[-1]["type"] == "done"
    assert recorder["executed"] == ["edit", "glob"]
    first_stream_seen = recorder["streams"][0]
    answered = {
        m["tool_call_id"] for m in first_stream_seen if m["role"] == "tool"
    }
    assert answered == {"call-bash", "call-edit", "call-glob"}


@pytest.mark.asyncio
async def test_stale_unpaired_round_is_not_auto_executed(recorder, monkeypatch):
    """An unpaired round buried under later messages (crashed turn followed
    by a fresh user turn) is dead history — never silently re-executed."""
    script_stream(monkeypatch, recorder)
    ctx = make_ctx(recorder)
    messages = paused_round_messages()
    messages.append({"role": "assistant", "content": "final words"})
    messages.append({"role": "user", "content": "new question"})

    events = await collect(LLMLoopRunner().run(ctx, messages))

    assert events[-1]["type"] == "done"
    assert recorder["executed"] == []
    starts = [e for e in events if e["type"] == "tool_start"]
    assert starts == []


def test_pending_tail_detection_shapes():
    detect = LLMLoopRunner._pending_tail_tool_calls
    messages = paused_round_messages()

    pending = detect(messages)
    assert [c["id"] for c in pending] == ["call-edit", "call-glob"]
    assert pending[0] == {
        "id": "call-edit", "name": "edit", "params": {"file_path": "a.txt"},
    }

    # Fresh turn / completed round / plain assistant tail: nothing pending.
    assert detect([{"role": "user", "content": "hi"}]) == []
    assert detect(messages + [{"role": "assistant", "content": "done"}]) == []
    assert detect(
        [{"role": "assistant", "content": "no calls, just text"}],
    ) == []
