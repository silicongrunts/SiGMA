"""``apply_cache_control`` marker placement: a system marker plus one tail
anchor.

The anchor scans backwards over every non-system, non-``_ephemeral``
message — tool, assistant, and user messages are all valid anchors — which
is what lets each tool-loop iteration's new messages join the cached
prefix incrementally. ``_ephemeral`` messages are skipped because they are
not persisted and their bytes can change between requests.
"""

import pytest

from app.core.message_format import apply_cache_control

MARKER = {"type": "ephemeral", "ttl": "1h"}


def test_anchor_on_newest_real_user_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "next"},
    ]

    apply_cache_control(messages, target_offset=0)

    assert messages[0]["cache_control"] == MARKER
    assert messages[3]["cache_control"] == MARKER
    assert "cache_control" not in messages[1]
    assert "cache_control" not in messages[2]


def test_anchor_moves_onto_tool_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
    ]

    apply_cache_control(messages, target_offset=0)

    assert messages[3]["cache_control"] == MARKER
    assert "cache_control" not in messages[1]
    assert "cache_control" not in messages[2]


def test_anchor_on_assistant_text_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "all done"},
    ]

    apply_cache_control(messages, target_offset=0)

    assert messages[2]["cache_control"] == MARKER
    assert "cache_control" not in messages[1]


def test_ephemeral_tail_is_skipped():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
        {"role": "user", "content": "<status>context warning</status>", "_ephemeral": True},
    ]

    apply_cache_control(messages, target_offset=0)

    assert messages[2]["cache_control"] == MARKER
    assert "cache_control" not in messages[3]


def test_stale_markers_are_cleared_everywhere():
    messages = [
        {"role": "system", "content": "sys", "cache_control": {"type": "ephemeral"}},
        {"role": "user", "content": "hi", "cache_control": MARKER},
        {"role": "assistant", "content": "ok", "cache_control": MARKER},
        {"role": "user", "content": "next"},
    ]

    apply_cache_control(messages, target_offset=0)

    # Only the system marker and the current anchor survive.
    assert messages[0]["cache_control"] == MARKER
    assert messages[3]["cache_control"] == MARKER
    assert "cache_control" not in messages[1]
    assert "cache_control" not in messages[2]


def test_offset_one_anchors_one_candidate_back():
    # Compaction shape: the one-off prompt closes the request and must not
    # become the anchor — the breakpoint rests on the last real history
    # message instead.
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "history"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "one-off compact prompt"},
    ]

    apply_cache_control(messages, target_offset=1)

    assert messages[2]["cache_control"] == MARKER
    assert "cache_control" not in messages[3]


def test_insufficient_candidates_keeps_system_marker_only():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "only turn"},
    ]

    apply_cache_control(messages, target_offset=1)

    assert messages[0]["cache_control"] == MARKER
    assert "cache_control" not in messages[1]


def test_no_system_message_still_anchors():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]

    apply_cache_control(messages, target_offset=0)

    assert messages[1]["cache_control"] == MARKER
    assert "cache_control" not in messages[0]


def test_mutates_the_passed_list_in_place():
    messages = [{"role": "user", "content": "hi"}]

    apply_cache_control(messages, target_offset=0)

    assert messages[0]["cache_control"] == MARKER
