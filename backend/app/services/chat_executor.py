"""Chat-streaming executor: assembles and drives one chat turn.

``ai_service`` launches chat tasks on the in-process task runtime; this
module builds the event source that the runtime consumes. It assembles the
token budget and runs ``QueryLoop``. Extracting the pipeline here keeps the
dependency graph acyclic: the function depends only on ``QueryLoop`` — never
on ``ai_service`` itself — so importing it from the submit paths cannot close
a cycle.

Session resolution is not repeated here: every submit path (new message,
resume, edit) resolves or creates the session once in ``ai_service`` and
passes its id in.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, Optional

from app.database.unit_of_work import UnitOfWork
from app.services.query_loop import QueryLoop
from app.services.token_budget import TokenBudgetTracker, TokenUsage


async def _load_turn_usage_baseline(
    project_id: str, session_id: str,
) -> tuple[TokenUsage, TokenUsage]:
    """Seed the turn token stats across pause/resume cycles.

    Each chat launch runs on a fresh in-memory tracker, so the whole-turn
    numbers are reassembled from two durable pieces: ``base`` — the spend
    already persisted after this turn's user message — and ``carry`` — the
    subagent spend recorded in the pending interaction checkpoint by the
    pause that split the turn (the subagent's own rows are not visible in
    the parent session until its agent tool completes). Both are zero for
    a fresh turn.
    """
    async with UnitOfWork(project_id) as uow:
        sums = await uow.messages.sum_usage_after_last_user(session_id)
        pending = await uow.task_state.get_pending_interaction_by_session(session_id)
    base = TokenUsage(
        input=int(sums.get("input") or 0),
        output=int(sums.get("output") or 0),
        cached=int(sums.get("cached") or 0),
    )
    carry_raw = (pending or {}).get("agent_usage_carry") or {}
    carry = TokenUsage(
        input=int(carry_raw.get("input") or 0),
        output=int(carry_raw.get("output") or 0),
        cached=int(carry_raw.get("cached") or 0),
    )
    return base, carry


async def stream_chat_for_task(
    *,
    project_id: str,
    context: Dict[str, Any],
    session_id: str,
    interaction_response: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    cancel_event: "asyncio.Event | None" = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run one chat turn end-to-end and yield ``{"type", "data"}`` events.

    The caller wraps the iterator in a ``task_runtime.launch`` source
    factory, supplying the cancel event; the runtime frames each yielded
    event into the task's stream session.
    """
    base, carry = await _load_turn_usage_baseline(project_id, session_id)
    token_budget_tracker = TokenBudgetTracker(
        context.get("token_budget"), base=base, carry=carry,
    )

    query_loop = QueryLoop(
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        interaction_response=interaction_response,
        cancel_event=cancel_event,
        token_budget_tracker=token_budget_tracker,
    )

    event_stream = (
        query_loop.compact_active()
        if context.get("compact_only")
        else query_loop.run()
    )
    async for event in event_stream:
        yield event
