"""Display semantics of the ``<memory_usage>`` tag.

Tags stay in stored assistant content — the model re-reads its own past
citations — so every consumer that shows or searches user-visible text must
strip them first. The patterns live here (core) so the service layer and the
chat-search helpers share one definition of the format; parsing the tags into
usage-log entries is memory-domain logic and stays in ``memory_service``.
"""

from __future__ import annotations

import re

# The tag name may be followed by attributes or stand bare — a malformed
# no-attribute tag must be stripped from display just like a well-formed one.
# Incomplete trailing tag while the assistant text is still streaming: the
# opening tag and everything after it is partial content, so it all goes.
MEMORY_USAGE_TAG_OPEN_RE = re.compile(r"<memory_usage(?:\s[^>]*)?>.*\Z", re.DOTALL)
_MEMORY_USAGE_TAG_DISPLAY_RE = re.compile(
    r"<memory_usage(?:\s[^>]*)?>.*?</memory_usage>\s*", re.DOTALL,
)
# A trailing fragment whose opening tag has not even closed with ">" yet —
# only possible mid-stream; strip it so raw markup never leaks into display.
_MEMORY_USAGE_TAG_PARTIAL_RE = re.compile(r"<memory_usage(?:\s[^>]*)?\Z", re.DOTALL)
# A closing tag whose opener never existed (or was already removed as part
# of a complete pair above) — stray markup, dropped.
_MEMORY_USAGE_TAG_STRAY_CLOSE_RE = re.compile(r"</memory_usage>\s*")


def strip_memory_usage_tags(text: str) -> str:
    """Remove complete and in-flight ``<memory_usage>`` tags for display."""
    if not text:
        return text
    stripped = _MEMORY_USAGE_TAG_DISPLAY_RE.sub("", text)
    stripped = MEMORY_USAGE_TAG_OPEN_RE.sub("", stripped)
    stripped = _MEMORY_USAGE_TAG_STRAY_CLOSE_RE.sub("", stripped)
    return _MEMORY_USAGE_TAG_PARTIAL_RE.sub("", stripped)
