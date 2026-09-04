"""Interactive-tool resume must deliver the user's modal response.

One merge contract serves the direct and subagent resume paths: the
checkpointed tool args are LLM output and get filtered to the tool's schema;
the interaction_response comes from the frontend modal and may only
introduce keys the schema does not declare. The runner-stamped context
params always win: a client response can never redirect a tool at another
project or session, nor swap a declared argument at answer time.
"""

from types import SimpleNamespace

import pytest
from importlib import import_module

import_module("app.agents.tools")
from app.agents.tools import plan_approval_tool
from app.agents.tools.registry import tool_registry
from app.core.message_format import is_failed_tool_result
from app.services.llm_loop_runner import LLMLoopRunner

pytestmark = pytest.mark.regression


@pytest.mark.asyncio
async def test_ask_user_question_resume_formats_answers():
    tool_def = tool_registry.get("ask_user_question")
    answers = [{"question": "Which database?", "answer": ["Postgres", "SQLite"]}]
    tool_args = {
        "questions": [{"question": "Which database?", "type": "multi"}],
        "hallucinated": "not in schema",
    }

    result = await LLMLoopRunner.call_interactive_tool(
        tool_def, tool_args, {"answers": answers},
    )

    # Literal expectation (not _format_answers(answers)) so a formatting
    # regression cannot hide behind the renderer under test.
    assert result == "Question 1: Which database?\nUser Answer: Postgres, SQLite"


@pytest.mark.asyncio
async def test_plan_approval_resume_saves_approved_plan(monkeypatch):
    async def fake_save_plan(project_id, session_id, plan_content):
        assert project_id == "proj1"
        assert session_id == "sess1"
        assert plan_content == "# Plan"
        return "sessions/sess1/plans/20260830-000000-abc123.md"

    monkeypatch.setattr(plan_approval_tool, "_save_plan", fake_save_plan)
    tool_def = tool_registry.get("submit_plan_for_approval")
    tool_args = {
        "plan_content": "# Plan",
        "project_id": "proj1",
        "session_id": "sess1",
        "hallucinated": "not in schema",
    }

    result = await LLMLoopRunner.call_interactive_tool(
        tool_def, tool_args, {"approved": True},
    )

    assert "approved and saved" in result
    assert "20260830-000000-abc123.md" in result


@pytest.mark.asyncio
async def test_interaction_response_cannot_override_context_params(monkeypatch):
    """A client response may carry the modal's payload keys but must not
    redirect the tool at another project or session: the runner-stamped
    context params from the checkpointed args are re-applied last."""
    saved = {}

    async def fake_save_plan(project_id, session_id, plan_content):
        saved["project_id"] = project_id
        saved["session_id"] = session_id
        return "sessions/sess1/plans/20260830-000000-abc123.md"

    monkeypatch.setattr(plan_approval_tool, "_save_plan", fake_save_plan)
    tool_def = tool_registry.get("submit_plan_for_approval")
    tool_args = {
        "plan_content": "# Plan",
        "project_id": "proj1",
        "session_id": "sess1",
    }

    result = await LLMLoopRunner.call_interactive_tool(
        tool_def, tool_args,
        {"approved": True, "project_id": "other-project", "session_id": "other-session"},
    )

    assert "approved and saved" in result
    assert saved == {"project_id": "proj1", "session_id": "sess1"}


@pytest.mark.asyncio
async def test_interaction_response_cannot_override_declared_args(monkeypatch):
    """A response key that collides with a declared schema property is
    dropped — the checkpoint's LLM-produced value is authoritative — while
    the modal's own keys (approved) still arrive."""
    saved = {}

    async def fake_save_plan(project_id, session_id, plan_content):
        saved["plan_content"] = plan_content
        return "sessions/sess1/plans/20260830-000000-abc123.md"

    monkeypatch.setattr(plan_approval_tool, "_save_plan", fake_save_plan)
    tool_def = tool_registry.get("submit_plan_for_approval")
    tool_args = {
        "plan_content": "# Plan from checkpoint",
        "project_id": "proj1",
        "session_id": "sess1",
    }

    result = await LLMLoopRunner.call_interactive_tool(
        tool_def, tool_args,
        {"approved": True, "plan_content": "# Swapped at approval time"},
    )

    assert "approved and saved" in result
    assert saved["plan_content"] == "# Plan from checkpoint"


@pytest.mark.asyncio
async def test_caller_can_restamp_session_for_session_scoped_tools(monkeypatch):
    """The subagent resume path re-stamps the session for tools that require
    one: the plan-approval tool saves the plan under the caller-chosen main
    session, not the checkpoint's agent session."""
    saved = {}

    async def fake_save_plan(project_id, session_id, plan_content):
        saved["session_id"] = session_id
        return "sessions/main-sess/plans/20260830-000000-abc123.md"

    monkeypatch.setattr(plan_approval_tool, "_save_plan", fake_save_plan)
    tool_def = tool_registry.get("submit_plan_for_approval")
    tool_args = {
        "plan_content": "# Plan",
        "project_id": "proj1",
        "session_id": "agent-session",
    }

    result = await LLMLoopRunner.call_interactive_tool(
        tool_def, tool_args, {"approved": True},
        session_id="main-session",
    )

    assert "approved and saved" in result
    assert saved["session_id"] == "main-session"


@pytest.mark.asyncio
async def test_interactive_tool_failure_returns_error_string():
    """On failure the rendered error string is the result — it becomes the
    persisted tool result (matching the repo-wide failed-result pattern), and
    no retry happens."""

    async def boom(**kwargs):
        raise RuntimeError("disk full")

    tool_def = SimpleNamespace(
        name="ask_user_question",
        input_schema={"properties": {"questions": {}}},
        call=boom,
    )

    result = await LLMLoopRunner.call_interactive_tool(
        tool_def, {"questions": [], "hallucinated": "x"}, {"answers": ["yes"]},
    )

    assert result == "Tool 'ask_user_question' error: disk full"
    assert is_failed_tool_result(result)
