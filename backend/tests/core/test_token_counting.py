"""build_assistant_turn token accounting: usage must never double-count."""

from app.core.message_format import (
    build_assistant_turn,
    finalize_assistant_turn,
)


class MockMsg:
    """Minimal mock for an ORM Message object."""

    def __init__(
        self,
        role,
        content="",
        tool_calls=None,
        tool_call_id=None,
        reasoning_content=None,
        token_count=0,
        cached_tokens=0,
        input_tokens=0,
        created_at=None,
    ):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.reasoning_content = reasoning_content
        self.token_count = token_count
        self.cached_tokens = cached_tokens
        self.input_tokens = input_tokens
        self.created_at = created_at


class TestTokenCount:
    """Verify that build_assistant_turn does not double-count tokens."""

    def test_single_assistant_token_count(self):
        """Single assistant with token_count=7 should produce 7, not 14."""
        messages = [
            MockMsg("assistant", "Hello", token_count=7, cached_tokens=2, input_tokens=3),
        ]
        turn, next_i = build_assistant_turn(messages, 0, {})
        assert turn["token_count"] == 7
        assert turn["cached_tokens"] == 2
        assert turn["input_tokens"] == 3

    def test_multi_assistant_token_count(self):
        """Three assistants: 7+5+3 = 15."""
        messages = [
            MockMsg("assistant", "Thinking...", token_count=7, cached_tokens=1, input_tokens=2),
            MockMsg("tool", "result", tool_call_id="tc1"),
            MockMsg("assistant", "More thinking", token_count=5, cached_tokens=2, input_tokens=1),
            MockMsg("assistant", "Final answer", token_count=3, cached_tokens=0, input_tokens=0),
        ]
        turn, next_i = build_assistant_turn(messages, 0, {})
        assert turn["token_count"] == 7 + 5 + 3
        assert turn["cached_tokens"] == 1 + 2 + 0
        assert turn["input_tokens"] == 2 + 1 + 0

    def test_tool_usage_is_included_in_assistant_turn_total(self):
        """Agent tool results carry subagent subtree usage and count in the bubble."""
        messages = [
            MockMsg("assistant", "Calling agent", token_count=10, cached_tokens=4, input_tokens=100),
            MockMsg("tool", "agent result", tool_call_id="call_agent",
                    token_count=80, cached_tokens=30, input_tokens=400),
            MockMsg("assistant", "Final answer", token_count=5, cached_tokens=2, input_tokens=50),
        ]
        turn, next_i = build_assistant_turn(messages, 0, {})
        assert next_i == len(messages)
        assert turn["token_count"] == 95
        assert turn["cached_tokens"] == 36
        assert turn["input_tokens"] == 550

    def test_finalize_preserves_token_count(self):
        """finalize_assistant_turn should not alter token totals."""
        messages = [
            MockMsg("assistant", "Answer", token_count=10, cached_tokens=3, input_tokens=4),
        ]
        turn, _ = build_assistant_turn(messages, 0, {})
        finalized = finalize_assistant_turn(turn)
        assert finalized["token_count"] == 10
        assert finalized["cached_tokens"] == 3
        assert finalized["input_tokens"] == 4
