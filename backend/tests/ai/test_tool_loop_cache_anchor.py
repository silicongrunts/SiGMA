"""Within-turn cache anchors: every tool-loop iteration re-anchors on the
latest non-system message, so the messages a tool round just produced join
the cached prefix for the next call instead of being re-billed in full."""

from importlib import import_module

import pytest

import_module("app.agents.tools")
from app.services.query_loop import QueryLoop
from tests.ai.matrix_harness import (  # noqa: F401 (autouse fixture below)
    collect,
    make_turn,
    script_llm,
    use_fixture_project_root,
)

MARKER = {"type": "ephemeral", "ttl": "1h"}


@pytest.mark.asyncio
async def test_tool_loop_iterations_anchor_on_latest_message(project, monkeypatch):
    """Call 1 anchors on the persisted user turn; call 2 re-anchors on the
    tool result. Markers never accumulate: one system marker plus one
    anchor per request.

    ``read`` of a missing file keeps the loop ungated (no permission pause)
    while still producing a real tool-result message for the second call.
    """
    calls = script_llm(monkeypatch, [
        ("tools", [{"id": "call_read", "name": "read", "params": {"file_path": "notes.md"}}]),
        ("text", "done"),
    ])
    session_id, task_id = await make_turn(project)

    await collect(QueryLoop(project, session_id, task_id=task_id))

    assert calls["n"] == 2
    first, second = calls["messages"]
    # Call 1: system marker + anchor on the user message.
    assert first[0]["role"] == "system"
    assert first[0]["cache_control"] == MARKER
    assert first[-1]["role"] == "user"
    assert first[-1]["cache_control"] == MARKER
    # Call 2: the anchor moved onto the tool result the first call produced,
    # so everything before it (including the user turn) is cached prefix.
    assert second[0]["role"] == "system"
    assert second[0]["cache_control"] == MARKER
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "call_read"
    assert second[-1]["cache_control"] == MARKER
    assert "cache_control" not in second[1]
    assert "cache_control" not in second[2]
    # Markers never accumulate — exactly one anchor per request.
    assert [m["role"] for m in second if "cache_control" in m] == ["system", "tool"]
