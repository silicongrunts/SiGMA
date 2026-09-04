"""The annotation toolset must stay non-interactive and agent-free.

The negative guards below fail only if a specific forbidden tool is added;
the positive assertion also fails if the toolset is ever emptied or renamed
wholesale, so "accidentally empty toolset == all green" cannot happen.
"""

from app.agents.toolsets import ANNOTATION_TOOLS, ALLOWED_AGENT_TYPES


def test_annotation_toolset_cannot_enter_interactive_or_subagent_state():
    # Positive anchor: the toolset keeps its real content.
    assert "read" in ANNOTATION_TOOLS
    assert ANNOTATION_TOOLS
    # Negative guards: no interactive or subagent entry points.
    assert "agent" not in ANNOTATION_TOOLS
    assert "ask_user_question" not in ANNOTATION_TOOLS
    assert "submit_plan_for_approval" not in ANNOTATION_TOOLS
    assert not ALLOWED_AGENT_TYPES["annotation"]
