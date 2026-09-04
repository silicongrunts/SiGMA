"""Task-kind contract and handler registry for the library background queue.

This module breaks what would otherwise be a circular import between
``services.background_task_service`` (the queue manager / dispatcher) and
the executors it invokes (``services.document_processing_service`` and
``services.index_builder``).  Those executors enqueue work via the queue
manager, and without this seam the queue manager would import them right
back to dispatch the work.

The contract here is intentionally tiny:

* Stable kind / queue string constants so both sides agree on names.
* The ``RunningTaskContext`` dataclass passed to every handler.
* A module-private registry populated by executors at import time and
  queried by the dispatcher.

Dependency direction after this seam: executors → ``library_task_protocol``
(registers handlers; may also import ``background_task_service`` to
enqueue) and ``background_task_service`` → ``library_task_protocol``
(looks up handlers).  Neither service imports the other to dispatch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from app.database.unit_of_work import UnitOfWork


# ---------------------------------------------------------------------------
# Stable constants
# ---------------------------------------------------------------------------

QUEUE_LIBRARY = "library"
KIND_DOCUMENT_PROCESS = "document_process"
KIND_RAG_INDEX = "rag_index"


# ---------------------------------------------------------------------------
# Per-task runtime context handed to handlers
# ---------------------------------------------------------------------------


@dataclass
class RunningTaskContext:
    """Runtime context for a single in-flight background task.

    Exposes the two operations every handler needs: refreshing the lease
    so the dispatcher does not reclaim the task, and detecting a cancel
    (user-initiated via the task row, or lease loss signalled in-process
    through ``cancel_event``).
    """

    project_id: str
    task_id: str
    owner: str
    lease_seconds: int
    # Set in-process when the task's lease was lost to another claim; the
    # durable row cannot carry that signal because the new claimant owns it.
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    async def heartbeat(self) -> bool:
        if self.cancel_event.is_set():
            return False
        try:
            async with UnitOfWork(self.project_id) as uow:
                return await uow.background_tasks.heartbeat(
                    self.task_id, self.owner, self.lease_seconds
                )
        except Exception:
            return False

    async def is_cancelling(self) -> bool:
        if self.cancel_event.is_set():
            return True
        try:
            async with UnitOfWork(self.project_id) as uow:
                return await uow.background_tasks.is_cancelling(self.task_id)
        except Exception:
            return True


# A handler takes the runtime context and the task's parsed payload.
TaskHandler = Callable[[RunningTaskContext, Dict[str, Any]], Awaitable[None]]


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------
#
# Executors (document_processing_service, index_builder) register their
# handlers at module import time.  Both modules are imported during
# application startup (see ``core/lifecycle``), so every kind is
# registered before the first task is dispatched.


_handlers: Dict[str, TaskHandler] = {}


def register_task_handler(kind: str, handler: TaskHandler) -> None:
    """Register *handler* as the executor for task *kind*.

    Idempotent: re-registering the same kind replaces the prior handler.
    A startup-time call from the executor module is the expected usage.
    """
    _handlers[kind] = handler


def get_task_handler(kind: str) -> Optional[TaskHandler]:
    """Return the registered handler for *kind*, or ``None`` if unknown."""
    return _handlers.get(kind)
