"""Shared helper for staging new LLM messages to the database.

Extracted from agent_service / query_loop / annotation_loop, which previously
duplicated this loop body nearly verbatim. The helper is concerned only with
the message-dict → DB-payload mapping; callers retain ownership of:

- UnitOfWork acquisition (``execute_atomic`` vs ``async with``)
- Slicing ``new_messages`` from the full LLM message list
- The group identifier (``session_id`` vs ``annotation_id``), bound via
  ``functools.partial``
- Any post-loop work (e.g. ``uow.sessions.stage_touch``)
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable


async def stage_new_messages(
    new_messages: list[dict[str, Any]],
    persist: Callable[..., Awaitable[None]],
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

        await persist(
            role=role,
            content=content,
            tool_calls=tool_calls_json,
            tool_call_id=msg.get("tool_call_id"),
            reasoning_content=msg.get("reasoning_content"),
            token_count=token_count,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
        )
