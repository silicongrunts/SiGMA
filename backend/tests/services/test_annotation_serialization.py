"""serialize_annotation: UI-facing annotation thread serialization."""

import json

from app.services.annotation_service import serialize_annotation


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


class MockAnnotation:
    """Minimal mock for an ORM Annotation object."""

    def __init__(self, id="anno-1", from_pos=0, to_pos=10,
                 original_text="hello", messages=None):
        self.id = id
        self.from_pos = from_pos
        self.to_pos = to_pos
        self.original_text = original_text
        self.messages = messages or []


class TestSerializeAnnotation:
    """Verify that serialize_annotation produces the expected UI structure."""

    def test_empty_annotation(self):
        """Annotation with no messages produces empty thread."""
        anno = MockAnnotation(id="a1", from_pos=5, to_pos=15,
                              original_text="sample")
        result = serialize_annotation(anno)
        assert result == {
            "id": "a1",
            "from": 5,
            "to": 15,
            "originalText": "sample",
            "thread": [],
        }

    def test_user_message(self):
        """User message appears in thread with normalized role 'user'."""
        anno = MockAnnotation(
            messages=[MockMsg("user", "Why does this work?")],
        )
        result = serialize_annotation(anno)
        assert len(result["thread"]) == 1
        assert result["thread"][0]["role"] == "user"
        assert result["thread"][0]["content"] == "Why does this work?"

    def test_assistant_with_tool_merge(self):
        """Assistant + tool messages merge into a single SiGMA turn."""
        tc_json = json.dumps([{
            "id": "tc1",
            "function": {"name": "search", "arguments": "query"},
        }])
        messages = [
            MockMsg("assistant", "Let me look...", tool_calls=tc_json,
                    token_count=7, cached_tokens=2, input_tokens=3),
            MockMsg("tool", "found it", tool_call_id="tc1"),
            MockMsg("assistant", "Here is the answer", token_count=5),
        ]
        anno = MockAnnotation(messages=messages)
        result = serialize_annotation(anno)
        assert len(result["thread"]) == 1
        turn = result["thread"][0]
        assert turn["role"] == "SiGMA"
        assert turn["content"] == "Here is the answer"
        assert turn["token_count"] == 7 + 5
        assert turn["cached_tokens"] == 2 + 0
        assert turn["input_tokens"] == 3 + 0
        # Process should contain tool step
        tools = [p for p in turn.get("process", []) if p["type"] == "tool"]
        assert len(tools) == 1
        assert tools[0]["tool"] == "search"

    def test_interrupted_turn_empty_bubble(self):
        """Turn ending with tool_calls (no final text) → empty bubble."""
        tc_json = json.dumps([{
            "id": "tc1",
            "function": {"name": "read", "arguments": "file"},
        }])
        messages = [
            MockMsg("assistant", "Reading...", tool_calls=tc_json, token_count=5),
            MockMsg("tool", "content", tool_call_id="tc1"),
        ]
        anno = MockAnnotation(messages=messages)
        result = serialize_annotation(anno)
        assert len(result["thread"]) == 1
        assert result["thread"][0]["content"] == ""  # interrupted → empty bubble

    def test_system_and_tool_messages_skipped(self):
        """System and standalone tool messages are not in the thread."""
        messages = [
            MockMsg("system", "boundary summary"),
            MockMsg("tool", "orphan result", tool_call_id="x"),
        ]
        anno = MockAnnotation(messages=messages)
        result = serialize_annotation(anno)
        assert result["thread"] == []

    def test_reasoning_is_not_serialized_to_ui(self):
        """Reasoning content is stored internally but not shown in UI."""
        messages = [
            MockMsg("assistant", "answer", reasoning_content="chain of thought",
                    token_count=3),
        ]
        anno = MockAnnotation(messages=messages)
        result = serialize_annotation(anno)
        assert "process" not in result["thread"][0]
        assert result["thread"][0]["content"] == "answer"
