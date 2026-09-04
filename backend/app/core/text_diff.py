"""Line-diff shaping and permission-payload caps shared by the permission
layer, file conflict checks, and chat-timeline file-edit cards.

Pure difflib helpers with no service or DB dependencies, so services and
``core.message_format`` share one implementation and one soft limit.
"""

from __future__ import annotations

import difflib

# Soft cap for diff payloads surfaced in UI modals (permission dialogs,
# file-edit timeline cards). Diffs beyond this are truncated, not refused.
DIFF_LINE_SOFT_LIMIT = 5000

# Soft cap (characters) for the flat preview content of a permission pause
# (write bodies, commands, notebook cell source). The pause payload lands in
# the stream buffer, every subscriber queue, and the persisted interaction
# checkpoint, so one huge write approval must not park a multi-megabyte
# frame in all of them. Content beyond this is truncated, not refused;
# ``content_truncated`` on the pause marks the cap.
CONTENT_SOFT_LIMIT = 20_000


def compute_diff_lines(old_text: str, new_text: str) -> list:
    """Compute a line-level diff between two texts using difflib.

    Returns ``{type, content}`` dicts where ``type`` is ``'context'``,
    ``'remove'``, or ``'add'``. Lines are compared without their line
    terminators, so a trailing-newline difference between the two texts
    cannot mark an otherwise identical line as removed and re-added.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    lines = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == 'equal':
            for line in old_lines[i1:i2]:
                lines.append({'type': 'context', 'content': line})
        elif op == 'replace':
            for line in old_lines[i1:i2]:
                lines.append({'type': 'remove', 'content': line})
            for line in new_lines[j1:j2]:
                lines.append({'type': 'add', 'content': line})
        elif op == 'delete':
            for line in old_lines[i1:i2]:
                lines.append({'type': 'remove', 'content': line})
        elif op == 'insert':
            for line in new_lines[j1:j2]:
                lines.append({'type': 'add', 'content': line})
    return lines
