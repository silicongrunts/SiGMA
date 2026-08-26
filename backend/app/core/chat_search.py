"""
Pure helpers for searching user-visible chat content.

Chat search must only match what the chat UI actually shows: user bubble
text (internal tags stripped) and the final assistant bubble of each turn.
Both are exactly what ``message_format.shape_messages_for_ui`` produces,
so search runs over its output instead of raw message rows — intermediate
process text (tool calls, hints, thinking) never matches by construction.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# UI entry roles whose bubble content is user-visible and searchable.
# Boundary summaries (``system``) are deliberately excluded.
SEARCHABLE_UI_ROLES = ("user", "SiGMA")

# Characters of context kept on each side of a match inside a snippet.
SNIPPET_CONTEXT = 60

_WHITESPACE_RE = re.compile(r"\s+")


def compile_search_pattern(query: str) -> re.Pattern:
    """Compile a case-insensitive substring pattern for *query*.

    A regex (not ``str.casefold().find``) is used because case folding can
    change string length (``'ß' → 'ss'``); regex match spans stay aligned
    with the original string, so snippet slicing cannot cut mid-match.
    """
    return re.compile(re.escape(query), re.IGNORECASE)


def sql_prefilter_reliable(query: str) -> bool:
    """Whether the repository's SQL ``lower()`` pre-filter can miss a hit.

    SQLite folds only ASCII letters. A non-ASCII cased letter in the query
    (``é``, ``Д``) may occur uppercase in stored rows and slip past the
    pre-filter, silently losing matches that Python-level matching — which
    folds the full Unicode range, like the title match — would find.
    Callers must scan sessions themselves when this returns False.
    """
    return all(
        ch.isascii() or not (ch.islower() or ch.isupper())
        for ch in query
    )


def build_snippet(content: str, match_start: int, match_end: int) -> str:
    """Return a single-line snippet around the [start, end) span."""
    start = max(0, match_start - SNIPPET_CONTEXT)
    end = min(len(content), match_end + SNIPPET_CONTEXT)
    snippet = _WHITESPACE_RE.sub(" ", content[start:end]).strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{snippet}{suffix}"


def find_visible_match(content: str, pattern: re.Pattern) -> Optional[Tuple[int, int]]:
    """Return the first visible-match span in *content*, or None."""
    if not content:
        return None
    found = pattern.search(content)
    return (found.start(), found.end()) if found else None


def visible_search_matches(
    entries: List[Dict[str, Any]], pattern: re.Pattern,
) -> List[Dict[str, Any]]:
    """Find *pattern* in user-visible entries from ``shape_messages_for_ui``.

    Returns one match dict per UI entry whose bubble content contains the
    needle: ``{id, role, seq, created_at, snippet}``.
    """
    matches: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("role") not in SEARCHABLE_UI_ROLES:
            continue
        span = find_visible_match(entry.get("content") or "", pattern)
        if span is None:
            continue
        matches.append({
            "id": entry["id"],
            "role": entry["role"],
            "seq": entry.get("seq"),
            "created_at": entry.get("created_at"),
            "snippet": build_snippet(entry["content"], *span),
        })
    return matches
