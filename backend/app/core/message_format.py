"""
Pure helpers for merging LLM message rows into UI-ready turn dicts.

Shared by:
* ``services/annotation_service.serialize_annotation()`` — annotation thread view
* ``services/ai_service.get_session_messages()`` / ``get_history()`` — session chat view

All helpers operate only on plain row-shaped objects (any object with the
expected ``role`` / ``content`` / ``tool_calls`` / ``tool_call_id``
attributes), so they have no service or DB dependencies and can live in
``core/``.  The two ``shape_*`` entry points compose the lower-level
``build_*`` helpers into full UI shaping and pagination.
"""

from __future__ import annotations

import json as _json
import re
from typing import Any, Dict, List, Optional

from app.core.chat_attachments import (
    extract_attachments,
    strip_image_refs_tag,
    strip_internal_image_tags,
)
from app.core.compaction_text import split_boundary_content
from app.core.text_diff import DIFF_LINE_SOFT_LIMIT, compute_diff_lines
from app.core.utils import to_iso


# Internal markup that may wrap user-submitted content; stripped before
# the message is sent to the chat bubble.
STATUS_TAG_RE = re.compile(r"<status>.*?</status>\s*", re.DOTALL)
CITATION_TAG_RE = re.compile(r"<citation>(.*?)</citation>\s*", re.DOTALL)


def build_tool_results_index(messages: list) -> Dict[str, str]:
    """Build a ``tool_call_id → content`` index from a message list."""
    results: Dict[str, str] = {}
    for m in messages:
        if getattr(m, "role", None) == "tool" and getattr(m, "tool_call_id", None):
            results[m.tool_call_id] = strip_image_refs_tag(m.content)
    return results


# "File edited: <path> (N replacement(s))" — N scales the badges for
# replace_all calls, where one old→new pair is applied N times.
_REPLACEMENT_COUNT_RE = re.compile(r"\((\d+) replacement\(s\)\)")


def is_failed_tool_result(result: str) -> bool:
    """True for the failure texts a tool result can carry: tool-layer
    ``Error:`` returns, the runner's exception wrapper ``Tool '<name>'
    error: …``, and mid-tool cancellation (``Tool cancelled by user.``)."""
    return (
        result.startswith(("Error:", "Tool cancelled by user."))
        or (result.startswith("Tool '") and " error: " in result)
    )


def file_edit_stats(
    tool_name: str, params: Any, *, result: str = ""
) -> Optional[Dict[str, Any]]:
    """Diff metadata for a completed ``edit``/``write`` tool call, or None.

    ``edit`` reports +adds/−dels line counts plus ``diff_lines`` for the
    timeline modal — the same typed-lines format and soft limit the
    permission dialog uses; the frontend renders it with char-level
    highlighting. For ``replace_all`` the counts are scaled by the
    replacement count parsed from the tool result. ``write`` reports the
    line count of the written content and the content itself (soft-capped)
    — the previous file content is not available at this layer, so no
    deletion side is claimed.

    Returns None for non-file tools, failed calls (``result`` carrying a
    failure marker — see ``is_failed_tool_result``), and calls without a
    usable path.
    """
    if tool_name not in ("edit", "write") or not isinstance(params, dict):
        return None
    if is_failed_tool_result(result):
        return None
    path = params.get("file_path") or params.get("path")
    if not path or not isinstance(path, str):
        return None

    if tool_name == "write":
        content = params.get("content")
        if not isinstance(content, str):
            return None
        lines = content.splitlines()
        return {
            "kind": "write",
            "path": path,
            "adds": len(lines),
            "content": "\n".join(lines[:DIFF_LINE_SOFT_LIMIT]),
            "content_truncated": len(lines) > DIFF_LINE_SOFT_LIMIT,
        }

    old, new = params.get("old_string"), params.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not old:
        return None
    diff_lines = compute_diff_lines(old, new)
    # Counts stay exact even when the shipped lines are capped below.
    adds = sum(1 for line in diff_lines if line["type"] == "add")
    dels = sum(1 for line in diff_lines if line["type"] == "remove")
    count_match = _REPLACEMENT_COUNT_RE.search(result)
    if count_match:
        count = int(count_match.group(1))
        adds *= count
        dels *= count
    truncated = len(diff_lines) > DIFF_LINE_SOFT_LIMIT
    if truncated:
        diff_lines = diff_lines[:DIFF_LINE_SOFT_LIMIT]
    return {
        "kind": "edit", "path": path, "adds": adds, "dels": dels,
        "diff_lines": diff_lines, "diff_truncated": truncated,
    }


def _parsed_edit_meta(
    tool_name: str, raw_args: str, result: str
) -> Optional[Dict[str, Any]]:
    """``file_edit_stats`` over persisted JSON tool arguments (None if unparsable)."""
    try:
        params = _json.loads(raw_args)
    except (_json.JSONDecodeError, TypeError):
        return None
    return file_edit_stats(tool_name, params, result=result)


def _assistant_turn_follows(messages: list, index: int) -> bool:
    """True if an assistant row appears after *index* before the next user
    or boundary row — i.e. the row at *index* is followed by (or starts) a
    turn that produced assistant output."""
    for m in messages[index + 1:]:
        role = getattr(m, "role", None)
        if role == "assistant":
            return True
        if role == "user" or (role == "system" and getattr(m, "is_boundary", False)):
            return False
    return False


def build_assistant_turn(
    messages: list,
    start_index: int,
    tool_results: Dict[str, str],
    *,
    truncate_params: int = 80,
    truncate_result: int = 200,
    truncate_hint: int = 500,
    parked: bool = False,
) -> tuple[Dict[str, Any], int]:
    """Merge consecutive assistant/tool messages starting at *start_index*.

    Returns ``(turn_dict, next_index)`` where *next_index* is the index of
    the first message not consumed by this turn (a user message or end of
    list).

    *parked* marks the turn as paused on an interaction checkpoint (the
    session's latest task is ``awaiting_input``). An unpaired tool call in a
    parked turn is the call the open dialog is blocked on — the turn is
    alive and its result arrives after the user answers, so it serializes
    as ``status: "awaiting_input"``, not ``"interrupted"``.

    The returned dict has the shape::

        {
            "full_content": str,        # last assistant text (for bubble)
            "process": [...],           # hints + tool steps
            "tool_calls": [...],        # raw parsed tool-call dicts
            "token_count": int,
            "cached_tokens": int,
            "input_tokens": int,
            "last_had_tool_calls": bool,  # True → turn interrupted mid-flight
        }
    """
    msg = messages[start_index]

    collected_process: List[Dict[str, Any]] = []
    collected_tool_calls: List[Dict[str, Any]] = []
    total_token_count: int = 0
    total_cached_tokens: int = 0
    total_input_tokens: int = 0
    assistant_texts: List[str] = []
    full_content = ""
    last_had_tool_calls = bool(getattr(msg, "tool_calls", None))

    def _add_usage(m):
        nonlocal total_token_count, total_cached_tokens, total_input_tokens
        total_token_count += getattr(m, "token_count", 0) or 0
        total_cached_tokens += getattr(m, "cached_tokens", 0) or 0
        total_input_tokens += getattr(m, "input_tokens", 0) or 0

    def _process_assistant(m):
        nonlocal full_content, last_had_tool_calls

        _add_usage(m)
        last_had_tool_calls = bool(getattr(m, "tool_calls", None))

        text = (getattr(m, "content", None) or "").strip()
        if text:
            truncated = text[:truncate_hint]
            assistant_texts.append(truncated)
            full_content = text
            collected_process.append({"type": "hint", "content": truncated})

        raw_tool_calls = getattr(m, "tool_calls", None)
        if raw_tool_calls:
            try:
                tcs = _json.loads(raw_tool_calls)
            except (_json.JSONDecodeError, TypeError):
                tcs = []
            collected_tool_calls.extend(tcs)
            for tc in tcs:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "") or ""
                tc_id = tc.get("id", "")
                has_result = bool(tc_id) and tc_id in tool_results
                if has_result:
                    status = "done"
                elif parked:
                    # The turn is parked on an interaction checkpoint and this
                    # call is the one the open dialog is blocking — alive, not
                    # dead; its result lands after the user answers.
                    status = "awaiting_input"
                else:
                    # No result and the session is not parked: the turn was
                    # cut off mid-flight (process restart mid-turn; startup
                    # reconciliation marks such tasks failed). It must not
                    # render as a completed step after a refresh.
                    status = "interrupted"
                step: Dict[str, Any] = {
                    "type": "tool",
                    "tool": fn.get("name", "unknown"),
                    "params": raw_args[:truncate_params],
                    "status": status,
                }
                # Emitted on every step so a resumed live stream can re-attach
                # to a history-reloaded step by id (tool_end flips it to done).
                if tc_id:
                    step["tool_call_id"] = tc_id
                if has_result:
                    r = tool_results[tc_id]
                    step["result"] = r[:truncate_result] + (
                        "..." if len(r) > truncate_result else ""
                    )
                    edit_meta = _parsed_edit_meta(fn.get("name", ""), raw_args, r)
                    if edit_meta:
                        step["fileEdit"] = edit_meta
                collected_process.append(step)

    # Process the first assistant message
    _process_assistant(msg)

    # Consume subsequent assistant/tool messages in this turn
    j = start_index + 1
    while j < len(messages):
        nxt = messages[j]
        role = getattr(nxt, "role", None)
        if role == "user":
            break
        if role == "assistant":
            _process_assistant(nxt)
        elif role == "tool":
            _add_usage(nxt)
        elif role == "system" and getattr(nxt, "is_boundary", False):
            if _assistant_turn_follows(messages, j):
                # Mid-turn compaction boundary: surface it as a timeline
                # step at its real position so the turn stays one entry.
                # The boundary row carries the summarization call's own
                # spend — part of this turn's totals.
                _add_usage(nxt)
                _, summary = split_boundary_content(getattr(nxt, "content", None) or "")
                collected_process.append({"type": "compact", "content": summary})
            else:
                # Boundary at the turn's tail belongs to the next turn —
                # stop here so the outer loop can attach it there.
                break
        j += 1

    return {
        "full_content": full_content,
        "process": collected_process,
        "tool_calls": collected_tool_calls,
        "token_count": total_token_count,
        "cached_tokens": total_cached_tokens,
        "input_tokens": total_input_tokens,
        "last_had_tool_calls": last_had_tool_calls,
        "assistant_texts": assistant_texts,
    }, j


def finalize_assistant_turn(turn: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw turn (from ``build_assistant_turn``) into a UI entry.

    Handles:
    * Promoting the last assistant text to bubble content (or leaving it
      empty for interrupted turns).
    * Removing the promoted hint from the process timeline.

    Returns a dict with keys: ``text``, ``process``, ``tool_calls``,
    ``token_count``, ``cached_tokens``, ``input_tokens``.
    """
    process = list(turn["process"])
    assistant_texts = turn["assistant_texts"]
    full_content = turn["full_content"]
    last_had_tool_calls = turn["last_had_tool_calls"]

    if last_had_tool_calls:
        # Turn was interrupted — keep bubble empty (frontend shows spinner)
        final_content = ""
    else:
        # Promote last hint to bubble, remove matching hint from process
        if assistant_texts:
            last_text = assistant_texts[-1]
            for idx in range(len(process) - 1, -1, -1):
                if (
                    process[idx].get("type") == "hint"
                    and process[idx].get("content") == last_text
                ):
                    process.pop(idx)
                    break
        final_content = full_content

    result: Dict[str, Any] = {"text": final_content}
    if process:
        result["process"] = process
    if turn["tool_calls"]:
        result["tool_calls"] = turn["tool_calls"]
    if turn["token_count"]:
        result["token_count"] = turn["token_count"]
    if turn["cached_tokens"]:
        result["cached_tokens"] = turn["cached_tokens"]
    if turn["input_tokens"]:
        result["input_tokens"] = turn["input_tokens"]
    return result


# ---------------------------------------------------------------------------
# Full UI shaping and pagination
# ---------------------------------------------------------------------------
#
# These compose the lower-level helpers above into the two operations the
# chat-history API needs: turning raw message rows into UI entries, and
# cursor-paginating those entries by user turn.  Both are pure: callers
# pass in the rows fetched from the repository.


def _boundary_card_entry(msg, summary: str) -> Dict[str, Any]:
    """Build the standalone UI entry for a compaction boundary card."""
    return {
        "id": msg.id,
        "role": "system",
        "content": summary,
        "token_count": getattr(msg, "token_count", 0),
        "cached_tokens": getattr(msg, "cached_tokens", 0),
        "input_tokens": getattr(msg, "input_tokens", 0),
        "is_boundary": True,
        "seq": msg.seq,
        "created_at": to_iso(msg.created_at),
    }


def shape_messages_for_ui(
    messages: list,
    boundary_seq: Optional[int],
    *,
    session_parked: bool = False,
) -> List[Dict[str, Any]]:
    """Group raw message rows into UI-ready chat-history entries.

    A turn is ``user → assistant* → tool_result* → ... → final assistant``.
    Only the last assistant message's content goes into the chat bubble;
    intermediate assistant thoughts and tool calls go into the ``process``
    timeline of the same entry.

    Compaction boundaries are shown without their LLM instruction prefix.
    An active boundary (``/compact``) becomes a standalone ``system`` card
    entry at its own position; a passive boundary becomes a ``compact``
    step leading the timeline of the assistant turn whose preparation
    triggered it, or a standalone card when no such turn exists.

    *boundary_seq* is the seq of the last compaction boundary; user
    messages at or before it are marked ``can_edit=False`` because the
    prior context has been summarised away.

    *session_parked* is the authoritative task-state fact that the
    session's latest task is stopped on an interaction checkpoint. It
    applies to the final assistant turn only — the park always lives
    there. An unpaired call in an earlier turn is a genuinely interrupted
    one.
    """
    tool_results = build_tool_results_index(messages)
    result: List[Dict[str, Any]] = []
    pending_compacts: List[tuple] = []

    # Start index of the final assistant turn: the first assistant row
    # after the last user row (the turn build consumes every assistant
    # row from there on, so this is the only start that can be parked).
    final_turn_start = -1
    if session_parked:
        last_user = -1
        for idx, m in enumerate(messages):
            if getattr(m, "role", None) == "user":
                last_user = idx
        for idx in range(last_user + 1, len(messages)):
            if getattr(messages[idx], "role", None) == "assistant":
                final_turn_start = idx
                break

    def _flush_pending() -> None:
        result.extend(
            _boundary_card_entry(row, summary) for row, summary in pending_compacts
        )
        pending_compacts.clear()

    i = 0
    while i < len(messages):
        msg = messages[i]

        # Standalone tool results are merged into the parent assistant turn.
        if msg.role == "tool":
            i += 1
            continue

        # Internal system messages are not shown; boundary summaries are.
        if msg.role == "system" and not msg.is_boundary:
            i += 1
            continue

        # Passive compaction fired while preparing the next turn — defer the
        # summary into that turn's timeline (see the assistant branch below).
        if msg.is_boundary:
            mode, summary = split_boundary_content(msg.content or "")
            if mode == "passive":
                pending_compacts.append((msg, summary))
                i += 1
                continue
            role_for_ui = "system"
        elif msg.role == "assistant":
            role_for_ui = "SiGMA"
        else:
            role_for_ui = msg.role

        # A user row whose turn never produced assistant output ends the
        # reach of a pending passive boundary — emit its card in position.
        if msg.role == "user" and pending_compacts and not _assistant_turn_follows(messages, i):
            _flush_pending()

        if msg.role == "user":
            citation_match = CITATION_TAG_RE.search(msg.content)
            citation_text = citation_match.group(1).strip() if citation_match else None
            attachments = extract_attachments(msg.content)
            clean_content = strip_internal_image_tags(
                CITATION_TAG_RE.sub("", STATUS_TAG_RE.sub("", msg.content))
            ).strip()
        elif msg.is_boundary:
            clean_content = summary
        else:
            citation_text = None
            attachments = []
            clean_content = msg.content

        entry: Dict[str, Any] = {
            "id": msg.id,
            "role": role_for_ui,
            "content": clean_content,
            "token_count": msg.token_count,
            "cached_tokens": msg.cached_tokens,
            "input_tokens": msg.input_tokens,
            "is_boundary": msg.is_boundary,
            "seq": msg.seq,
            "created_at": to_iso(msg.created_at),
        }
        if citation_text:
            entry["citation"] = citation_text
        if attachments:
            entry["attachments"] = attachments
        if msg.role == "user":
            entry["can_edit"] = boundary_seq is None or msg.seq > boundary_seq

        if msg.role == "assistant":
            turn, next_i = build_assistant_turn(
                messages, i, tool_results,
                truncate_params=80,
                truncate_result=200,
                truncate_hint=500,
                parked=i == final_turn_start,
            )
            finalized = finalize_assistant_turn(turn)
            entry["content"] = finalized.get("text", "")
            if "process" in finalized:
                entry["process"] = finalized["process"]
            if "tool_calls" in finalized:
                entry["tool_calls"] = finalized["tool_calls"]
            entry["token_count"] = turn["token_count"]
            entry["cached_tokens"] = turn["cached_tokens"]
            entry["input_tokens"] = turn["input_tokens"]
            if pending_compacts:
                # Compaction fired while this turn was being prepared: lead
                # its timeline with the summary step, matching where the
                # live stream showed the compaction hint. The boundary rows'
                # summarization spend belongs to this turn too — it happened
                # after the user message, before the first assistant call.
                for boundary_msg, _ in pending_compacts:
                    entry["token_count"] += getattr(boundary_msg, "token_count", 0) or 0
                    entry["cached_tokens"] += getattr(boundary_msg, "cached_tokens", 0) or 0
                    entry["input_tokens"] += getattr(boundary_msg, "input_tokens", 0) or 0
                entry["process"] = [
                    {"type": "compact", "content": summary}
                    for _, summary in pending_compacts
                ] + entry.get("process", [])
                pending_compacts.clear()
            i = next_i
        else:
            i += 1

        result.append(entry)

    # Boundaries never followed by any turn (compaction at session end).
    _flush_pending()
    return result


def page_ui_turns(
    entries: List[Dict[str, Any]],
    *,
    limit: int,
    before_seq: Optional[int],
) -> Dict[str, Any]:
    """Return up to *limit* user turns plus pagination metadata.

    Pagination is based on user turns, not raw entries.  *before_seq* is
    the seq of the earliest loaded user turn; the next page returns user
    turns older than that seq.  Boundary notes are included by their real
    seq position so merging pages by seq keeps compaction summaries
    ordered correctly.
    """
    user_turn_indices = [
        idx for idx, entry in enumerate(entries)
        if entry.get("role") == "user"
    ]
    if not user_turn_indices:
        return {"messages": [], "has_more": False, "next_before_seq": None}

    total = len(user_turn_indices)
    if before_seq is None:
        end_count = total
    else:
        end_count = 0
        for idx in user_turn_indices:
            seq = entries[idx].get("seq")
            if isinstance(seq, int) and seq < before_seq:
                end_count += 1
            else:
                break
    if end_count <= 0:
        return {"messages": [], "has_more": False, "next_before_seq": None}

    start_count = max(0, end_count - limit)
    has_more = start_count > 0

    start_idx = user_turn_indices[start_count]
    end_idx = user_turn_indices[end_count] if end_count < total else len(entries)

    boundary_notes = [
        entry for entry in entries[:start_idx]
        if entry.get("is_boundary")
    ]
    window = boundary_notes + entries[start_idx:end_idx]
    earliest_user = next(
        (entry for entry in window if entry.get("role") == "user"),
        None,
    )
    next_before_seq = earliest_user.get("seq") if has_more and earliest_user else None
    return {
        "messages": window,
        "has_more": has_more,
        "next_before_seq": next_before_seq,
    }
