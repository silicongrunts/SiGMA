"""Compaction boundary text shared by the writer and the UI shaper.

``compaction_service`` persists boundary rows whose content starts with a
mode-specific instruction prefix addressed to the next LLM call.
``message_format`` strips the same prefixes before the summary reaches the
UI. Both prefixes live here so the write side and the read side cannot drift
apart.
"""

from __future__ import annotations

from typing import Optional, Tuple


PASSIVE_SUMMARY_PREFIX = """This session was compacted automatically because the context exceeded the configured threshold.

Continue the user's latest request using the summary below. Do not ask the user to repeat information already captured here.

"""


ACTIVE_SUMMARY_PREFIX = """The user explicitly requested /compact. This session summary is now the active handoff context.

When the user sends the next request, continue from this summary and the subsequent messages.

"""


def split_boundary_content(content: str) -> Tuple[Optional[str], str]:
    """Split a boundary row's content into ``(mode, summary)``.

    *mode* is ``"active"`` or ``"passive"`` when *content* starts with the
    matching instruction prefix; the prefix is stripped from the returned
    summary. For content without a known prefix (legacy or future rows) the
    mode is ``None`` and the content is returned unchanged — callers keep
    treating it as a standalone boundary note.
    """
    for mode, prefix in (("active", ACTIVE_SUMMARY_PREFIX), ("passive", PASSIVE_SUMMARY_PREFIX)):
        if content.startswith(prefix):
            return mode, content[len(prefix):].strip()
    return None, content
