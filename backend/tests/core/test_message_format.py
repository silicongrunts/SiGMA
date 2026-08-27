"""Message shaping: turning raw message rows into UI chat-history entries."""

from types import SimpleNamespace

from app.core.compaction_text import (
    ACTIVE_SUMMARY_PREFIX,
    PASSIVE_SUMMARY_PREFIX,
    split_boundary_content,
)
from app.core.message_format import page_ui_turns, shape_messages_for_ui


def _row(seq, role, content="", *, boundary=False, tool_calls=None, tool_call_id=None):
    return SimpleNamespace(
        id=f"m{seq}",
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        token_count=0,
        cached_tokens=0,
        input_tokens=0,
        is_boundary=boundary,
        seq=seq,
        created_at=None,
    )


def _turn(seq, content="answer"):
    """Minimal completed assistant turn (single text row)."""
    return _row(seq, "assistant", content)


def test_split_boundary_content_recognises_both_modes():
    mode, summary = split_boundary_content(ACTIVE_SUMMARY_PREFIX + "summary body")
    assert mode == "active"
    assert summary == "summary body"

    mode, summary = split_boundary_content(PASSIVE_SUMMARY_PREFIX + "summary body")
    assert mode == "passive"
    assert summary == "summary body"

    mode, summary = split_boundary_content("legacy boundary without a prefix")
    assert mode is None
    assert summary == "legacy boundary without a prefix"


def test_active_boundary_becomes_standalone_card_without_instructions():
    messages = [
        _row(1, "user", "previous question"),
        _turn(2),
        _row(3, "user", "start Random Capability Audit section"),
        _row(4, "system", ACTIVE_SUMMARY_PREFIX + "## 摘要正文", boundary=True),
    ]
    entries = shape_messages_for_ui(messages, 4)

    card = entries[-1]
    assert card["role"] == "system"
    assert card["is_boundary"] is True
    assert card["content"] == "## 摘要正文"
    assert "The user explicitly requested" not in card["content"]
    assert [e["seq"] for e in entries] == [1, 2, 3, 4]


def test_passive_boundary_leads_next_turn_process():
    messages = [
        _turn(10, "final reply of previous turn"),
        _row(11, "system", PASSIVE_SUMMARY_PREFIX + "summary body", boundary=True),
        _row(12, "user", "next question"),
        _turn(13),
    ]
    entries = shape_messages_for_ui(messages, 11)

    assert [e["role"] for e in entries] == ["SiGMA", "user", "SiGMA"]
    assert entries[0]["seq"] == 10
    next_turn = entries[-1]
    assert next_turn["process"] == [{"type": "compact", "content": "summary body"}]


def test_mid_turn_boundary_keeps_single_turn_with_step_in_position():
    tool_calls = '[{"id": "t1", "function": {"name": "read_file", "arguments": "{}"}}]'
    messages = [
        _row(20, "user", "go"),
        _row(21, "assistant", "", tool_calls=tool_calls),
        _row(22, "tool", "file body", tool_call_id="t1"),
        _row(23, "system", PASSIVE_SUMMARY_PREFIX + "mid summary", boundary=True),
        _turn(24),
    ]
    entries = shape_messages_for_ui(messages, None)

    assert len(entries) == 2
    turn = entries[-1]
    types = [step["type"] for step in turn["process"]]
    assert types == ["tool", "compact"]
    assert turn["process"][1]["content"] == "mid summary"


def test_passive_boundary_without_following_turn_falls_back_to_card():
    messages = [
        _turn(30, "reply"),
        _row(31, "system", PASSIVE_SUMMARY_PREFIX + "s", boundary=True),
        _row(32, "user", "message of a stopped turn"),
    ]
    entries = shape_messages_for_ui(messages, 31)

    cards = [e for e in entries if e.get("is_boundary")]
    assert len(cards) == 1
    assert cards[0]["content"] == "s"
    # The card sits between the last reply and the stopped turn's message.
    assert [e["seq"] for e in entries] == [30, 31, 32]


def test_non_boundary_system_messages_stay_hidden():
    messages = [
        _row(40, "user", "question"),
        _row(41, "system", "internal reminder"),
        _turn(42),
    ]
    entries = shape_messages_for_ui(messages, None)
    assert [e["seq"] for e in entries] == [40, 42]


def test_boundary_cards_survive_pagination_before_the_window():
    entries = (
        [{"id": "c", "role": "system", "content": "s", "is_boundary": True, "seq": 1}]
        + [
            {"id": f"u{i}", "role": "user", "content": "q", "is_boundary": False, "seq": i * 10}
            for i in range(12)
        ]
    )
    page = page_ui_turns(entries, limit=5, before_seq=None)
    boundary_seqs = [e["seq"] for e in page["messages"] if e.get("is_boundary")]
    assert boundary_seqs == [1]
    assert page["messages"][0]["seq"] == 1
