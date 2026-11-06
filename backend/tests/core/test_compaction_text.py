"""Boundary content splitting: the memory re-injection block stays LLM-only.

The compaction boundary row bakes in a trailing ``<memory>`` block so the
model still sees the index after compaction; the UI card must not show it.
"""

from app.core.compaction_text import (
    ACTIVE_SUMMARY_PREFIX,
    PASSIVE_SUMMARY_PREFIX,
    split_boundary_content,
)

MEMORY_BLOCK = "\n\n<memory>\n- a.md — user profile\n</memory>\n"


def test_passive_boundary_strips_trailing_memory_block():
    content = PASSIVE_SUMMARY_PREFIX + "Summary prose." + MEMORY_BLOCK
    mode, summary = split_boundary_content(content)
    assert mode == "passive"
    assert summary == "Summary prose."
    assert "<memory>" not in summary


def test_active_boundary_strips_trailing_memory_block():
    content = ACTIVE_SUMMARY_PREFIX + "Handoff summary." + MEMORY_BLOCK
    mode, summary = split_boundary_content(content)
    assert mode == "active"
    assert summary == "Handoff summary."


def test_boundary_without_memory_block_unchanged():
    mode, summary = split_boundary_content(PASSIVE_SUMMARY_PREFIX + "Just a summary.")
    assert mode == "passive"
    assert summary == "Just a summary."


def test_legacy_boundary_strips_memory_block_keeps_rest():
    content = "Legacy boundary note." + MEMORY_BLOCK
    mode, summary = split_boundary_content(content)
    assert mode is None
    assert summary == "Legacy boundary note."


def test_memory_mention_inside_prose_not_touched():
    # The strip regex is anchored at the string end: a "<memory>" mention in
    # the middle of the summary prose is displayed, not stripped.
    prose = "Discussed the <memory> tag format. Conclusions follow."
    mode, summary = split_boundary_content(PASSIVE_SUMMARY_PREFIX + prose)
    assert mode == "passive"
    assert "<memory> tag format" in summary


def test_complete_memory_pair_in_prose_survives_trailing_strip():
    # A full <memory>...</memory> pair quoted inside the prose must not let
    # the strip run from it to the end of the string: the pattern's body is
    # tempered to stop at either tag, so only the trailing injection block
    # (whose sanitized body never contains the tags) is removed.
    prose = (
        "Quoted the injected <memory>index snippet</memory> in passing. "
        "Conclusions follow."
    )
    trailing = "\n\n<memory>\nMEMORY.md index\n</memory>\n"
    mode, summary = split_boundary_content(PASSIVE_SUMMARY_PREFIX + prose + trailing)
    assert mode == "passive"
    assert "<memory>index snippet</memory> in passing" in summary
    assert "MEMORY.md index" not in summary
