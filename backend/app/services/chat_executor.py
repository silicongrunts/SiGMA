"""Chat-streaming executor for Huey-driven LLM tasks.

``workers.huey_tasks.run_llm_chat`` and ``services.ai_service.AIService.stream_chat``
share the same streaming pipeline: resolve or create a session, run
``QueryLoop``, and emit HTTP Server-Sent-Events chunks.  Extracting the
pipeline here keeps the dependency graph acyclic: without this seam,
the service would import the worker (to enqueue) while the worker
imports the service right back (to execute).

The function depends only on ``QueryLoop`` and the database session —
never on ``ai_service`` itself — so the dependency graph stays a clean
DAG: ``ai_service → huey_tasks`` (enqueue) and ``huey_tasks →
chat_executor`` (execute).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, Optional

from app.database.unit_of_work import UnitOfWork
from app.services.query_loop import QueryLoop
from app.services.token_budget import TokenBudgetTracker


def _format_sse(event: str, data: dict) -> str:
    """Format an HTTP Server-Sent-Events chunk."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_chat_for_task(
    *,
    project_id: str,
    context: Dict[str, Any],
    session_id: Optional[str] = None,
    interaction_response: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    cancel_event: "asyncio.Event | None" = None,
) -> AsyncGenerator[str, None]:
    """Run one chat turn end-to-end and yield SSE-formatted event strings.

    The caller (the Huey worker's streaming runner) is responsible for
    relaying each yielded string to the stream server.
    """
    async with UnitOfWork(project_id) as uow:
        if session_id:
            db_session = await uow.sessions.get_by_id(session_id)
            if db_session is None:
                session_id = None

        if session_id is None:
            db_session = await uow.sessions.create(project_id)
            session_id = db_session.id

    token_budget_tracker = TokenBudgetTracker(context.get("token_budget"))

    query_loop = QueryLoop(
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        interaction_response=interaction_response,
        cancel_event=cancel_event,
        token_budget_tracker=token_budget_tracker,
    )

    yield _format_sse("thought", {"message": "Processing..."})

    event_stream = (
        query_loop.compact_active()
        if context.get("compact_only")
        else query_loop.run()
    )
    async for event in event_stream:
        yield _format_sse(event["type"], event["data"])
