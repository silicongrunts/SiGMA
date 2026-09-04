"""Shared helper for staging new LLM messages to the database.

The helper is concerned only with the message-dict → DB-payload mapping;
callers retain ownership of:

- UnitOfWork acquisition (``execute_atomic`` vs ``async with``)
- Slicing ``new_messages`` from the full LLM message list
- The group identifier (``session_id`` vs ``annotation_id``), bound via
  ``functools.partial``
- Any post-loop work (e.g. ``uow.sessions.stage_touch``)

It also implements the partial-assistant checkpoint protocol used by
``LLMLoopRunner`` to durably persist a long assistant turn while it streams:
the in-flight assistant message is marked with ``_checkpoint``, inserted once
at its final seq position, and refreshed in place on later flushes. The
protocol state lives on the message dict itself (``_checkpoint_row_id``), so
the round-boundary save can update the same row instead of inserting a
duplicate, without any shared mutable tracker object.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

# Marks a dict as the in-flight streamed assistant message (set by the runner,
# in-memory only — never part of the DB payload).
CHECKPOINT_FLAG = "_checkpoint"
# Row id stamped onto a flagged dict after its first insert.
CHECKPOINT_ROW_ID = "_checkpoint_row_id"


async def stage_new_messages(
    new_messages: list[dict[str, Any]],
    persist: Callable[..., Awaitable[Any]],
    update: "Callable[..., Awaitable[bool]] | None" = None,
) -> None:
    """Filter, extract fields, and persist each new message via ``persist``.

    Skips ephemeral (in-memory-only) and system messages, then for each
    remaining message extracts the standard payload fields and calls
    ``persist(**payload)``. The caller binds the group identifier on
    ``persist`` (e.g. ``functools.partial(uow.messages.stage_create,
    session_id=...)``).

    ``assistant`` and ``tool`` roles both carry token accounting fields
    (``_completion_tokens`` / ``_input_tokens`` / ``_cached_tokens``);
    other roles default to zero token counts.

    Checkpoint protocol: a message dict flagged with ``CHECKPOINT_FLAG`` is
    the in-flight streamed assistant message. On first persist it is inserted
    and its row id is stamped back onto the dict; on every later persist (row
    id present) the existing row is refreshed via ``update`` — never inserted
    again, which would duplicate the message and shift the history_count
    slice. When ``update`` is None a flagged dict with a row id is skipped:
    the row already exists and duplicating it is the one unrecoverable failure.
    """
    for msg in new_messages:
        if msg.get("_ephemeral") or msg.get("role", "") == "system":
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")
        tool_calls_json = None
        if msg.get("tool_calls"):
            tool_calls_json = json.dumps(msg["tool_calls"], ensure_ascii=False)

        if role in ("assistant", "tool"):
            token_count = int(msg.get("_completion_tokens") or 0)
            input_tokens = int(msg.get("_input_tokens") or 0)
            cached_tokens = int(msg.get("_cached_tokens") or 0)
        else:
            token_count = 0
            input_tokens = 0
            cached_tokens = 0

        row_id = msg.get(CHECKPOINT_ROW_ID)
        if row_id is not None:
            if update is None:
                logger.warning(
                    "Checkpointed message row %s cannot be refreshed: no "
                    "update callback provided; skipping to avoid a duplicate",
                    row_id,
                )
                continue
            updated = await update(
                row_id,
                content=content,
                tool_calls=tool_calls_json,
                reasoning_content=msg.get("reasoning_content"),
                token_count=token_count,
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
            )
            if updated:
                continue
            # The checkpoint row vanished (history rewritten underneath the
            # run) — fall through to a normal insert so the content is kept.
            logger.warning(
                "Checkpoint row %s is missing; re-inserting message", row_id,
            )
            msg.pop(CHECKPOINT_ROW_ID, None)

        row = await persist(
            role=role,
            content=content,
            tool_calls=tool_calls_json,
            tool_call_id=msg.get("tool_call_id"),
            reasoning_content=msg.get("reasoning_content"),
            token_count=token_count,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
        )
        if msg.get(CHECKPOINT_FLAG) and row is not None:
            msg[CHECKPOINT_ROW_ID] = row.id


def clear_checkpoint_markers(messages: list[dict[str, Any]]) -> None:
    """Drop the in-memory checkpoint markers from messages whose final content
    was persisted in place.

    Called at the round boundary by the runner: without this the markers
    would survive for the rest of the turn and every later save would
    re-update already-final rows. Stripped messages are then skipped by the
    history-count slice like any other persisted row.
    """
    for msg in messages:
        msg.pop(CHECKPOINT_ROW_ID, None)
        msg.pop(CHECKPOINT_FLAG, None)
