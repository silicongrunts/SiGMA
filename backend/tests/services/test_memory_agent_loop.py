"""The bounded memory agent loop: policy denial, real writes, budgets, no-ops."""

import asyncio
import json

import pytest

from app.core.config import settings
from app.services import llm_service as llm_service_module
from app.services.memory_agent_loop import (
    CONSOLIDATION_ALLOWED_TOOLS,
    EXTRACTION_ALLOWED_TOOLS,
    NOTHING_TO_SAVE,
    run_memory_agent_loop,
)


def _tool_call(name, params, call_id="call-1"):
    return {"id": call_id, "name": name, "params": params}


@pytest.fixture
def memory_tree(project):
    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _scripted_llm(monkeypatch, responses, calls):
    """Replace ``llm_service.stream_chat`` with a scripted fake.

    The request's message list is snapshotted per call: the loop mutates its
    working list in place, so a live reference would show the final state,
    not the state each call saw.
    """

    async def fake_stream_chat(**kwargs):
        calls.append({**kwargs, "messages": list(kwargs["messages"])})
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        text, tool_calls = response
        return text, "", tool_calls, {"total_tokens": 1}

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", fake_stream_chat)


async def test_no_tool_calls_finishes_in_one_turn(project, memory_tree, monkeypatch):
    calls: list = []
    _scripted_llm(monkeypatch, [("Nothing to save.", [])], calls)

    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
    )
    assert result.turns == 1
    assert result.is_noop is True
    assert result.final_text == NOTHING_TO_SAVE
    assert len(calls) == 1
    # sticky provider session routing for the memory agent
    assert calls[0]["session_id"] == f"memory:{project}"


async def test_routing_session_id_overrides_the_default(project, memory_tree, monkeypatch):
    """Extraction routes through the replayed chat session so its identical
    prefix lands on the instance holding the main loop's provider cache."""
    calls: list = []
    _scripted_llm(monkeypatch, [("Nothing to save.", [])], calls)

    await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
        routing_session_id="chat-42",
    )
    assert calls[0]["session_id"] == "chat-42"


async def test_write_inside_memory_root_executes(project, memory_tree, monkeypatch):
    calls: list = []
    target = str(memory_tree / "user_name.md")
    _scripted_llm(monkeypatch, [
        ("Saving.", [_tool_call("write", {
            "file_path": target,
            "content": "---\ndescription: user name\n---\n\nThe user is 张三.",
        }, "call-w")]),
        ("Done.", []),
    ], calls)

    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
    )
    assert result.turns == 2 and result.tool_error_count == 0
    saved = memory_tree / "user_name.md"
    assert saved.exists() and "张三" in saved.read_text(encoding="utf-8")
    # the transcript carries the assistant tool call + the tool result
    followup = calls[1]["messages"]
    assert followup[-1]["role"] == "tool" and "user_name.md" in followup[-1]["content"]


async def test_denied_tool_gets_error_not_execution(project, memory_tree, monkeypatch):
    calls: list = []
    _scripted_llm(monkeypatch, [
        ("Trying bash.", [_tool_call("bash", {"command": "rm -rf /tmp/x"}, "call-b")]),
        ("Understood.", []),
    ], calls)

    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
    )
    assert result.tool_error_count == 1
    tool_msg = calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "denied for the memory agent" in tool_msg["content"]


async def test_mutating_tool_outside_memory_root_denied(project, memory_tree, monkeypatch):
    calls: list = []
    _scripted_llm(monkeypatch, [
        ("Writing.", [_tool_call("write", {
            "file_path": "notes.md", "content": "not memory",
        }, "call-o")]),
        ("Ok.", []),
    ], calls)

    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
    )
    assert result.tool_error_count == 1
    tool_msg = calls[1]["messages"][-1]
    assert "inside the project memory directory" in tool_msg["content"]
    assert not (settings.get_project_path(project) / "notes.md").exists()


async def test_rm_allowed_for_consolidation_only(project, memory_tree, monkeypatch):
    (memory_tree / "old.md").write_text("stale\n", encoding="utf-8")
    calls: list = []
    _scripted_llm(monkeypatch, [
        ("Removing.", [_tool_call("rm", {"path": ".SiGMA/memory/old.md"}, "call-r")]),
        ("Nothing to change.", []),
    ], calls)

    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "consolidate"}],
        tool_schemas=[],
        allowed_tools=CONSOLIDATION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
    )
    assert result.tool_error_count == 0
    assert not (memory_tree / "old.md").exists()

    # the extraction agent cannot rm even when the model tries
    (memory_tree / "keep.md").write_text("keep\n", encoding="utf-8")
    calls2: list = []
    _scripted_llm(monkeypatch, [
        ("Removing.", [_tool_call("rm", {"path": ".SiGMA/memory/keep.md"}, "call-r2")]),
        ("Fine.", []),
    ], calls2)
    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
    )
    assert result.tool_error_count == 1
    assert (memory_tree / "keep.md").exists()


async def test_memory_mutations_record_landed_writes_only(project, memory_tree, monkeypatch):
    """The result lists resolved paths of mutating calls that landed, so the
    extraction handler can deplete the consolidation counter; a denied or
    failed write must not count as new material."""
    calls: list = []
    fact = str(memory_tree / "user_name.md")
    _scripted_llm(monkeypatch, [
        ("Saving.", [
            _tool_call("write", {
                "file_path": fact,
                "content": "---\ndescription: user name\n---\n\nThe user is 张三.",
            }, "call-w"),
            # outside the memory root: policy-denied, never executes
            _tool_call("write", {
                "file_path": "notes.md", "content": "not memory",
            }, "call-x"),
        ]),
        ("Fixing.", [_tool_call("edit", {
            "file_path": fact, "old_string": "张三", "new_string": "张三（Zhang San）",
        }, "call-e")]),
        ("Nothing to save.", []),
    ], calls)

    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
    )
    assert result.tool_error_count == 1
    # both landed calls on the same file are reported, in call order; the
    # denied write is absent (the handler deduplicates per file)
    assert result.memory_mutations == [fact, fact]


async def test_turn_budget_returns_partial(project, memory_tree, monkeypatch):
    calls: list = []

    async def endless_tool_calls(**kwargs):
        calls.append(kwargs)
        return "still working", "", [_tool_call("ls", {"path": "."}, f"call-{len(calls)}")], None

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", endless_tool_calls)

    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=3,
        max_tokens=100,
    )
    assert result.turns == 3
    assert len(calls) == 3


async def test_cancel_event_aborts_loop(project, memory_tree, monkeypatch):
    cancel_event = asyncio.Event()
    cancel_event.set()

    async def never_called(**kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("LLM called after cancellation")

    monkeypatch.setattr(llm_service_module.llm_service, "stream_chat", never_called)

    with pytest.raises(asyncio.CancelledError):
        await run_memory_agent_loop(
            project_id=project,
            messages=[{"role": "user", "content": "extract"}],
            tool_schemas=[],
            allowed_tools=EXTRACTION_ALLOWED_TOOLS,
            max_turns=2,
            max_tokens=100,
            cancel_event=cancel_event,
        )


async def test_json_string_params_are_accepted(project, memory_tree, monkeypatch):
    calls: list = []
    _scripted_llm(monkeypatch, [
        ("Saving.", [{
            "id": "call-j", "name": "write",
            "params": json.dumps({
                "file_path": str(memory_tree / "j.md"), "content": "json args",
            }),
        }]),
        ("Done.", []),
    ], calls)

    result = await run_memory_agent_loop(
        project_id=project,
        messages=[{"role": "user", "content": "extract"}],
        tool_schemas=[],
        allowed_tools=EXTRACTION_ALLOWED_TOOLS,
        max_turns=5,
        max_tokens=100,
    )
    assert result.tool_error_count == 0
    assert (memory_tree / "j.md").read_text(encoding="utf-8") == "json args"
