"""Shared task lifecycle constants.

The task status column (``TaskState.status`` / ``BackgroundTask.status``) is a
free-form string shared across the database layer, services, the worker, and the
stream relay. The SSE terminal event names are a wire contract with the frontend.
Centralizing both here prevents silent drift: a typo such as ``"canceling"``
would otherwise break the terminal-detection short-circuits spread across
several modules without any compile-time signal.

This module holds the canonical definitions; ``background_task_repo`` and
``llm_loop_runner`` re-export them for backward compatibility.
"""

# --- Task lifecycle statuses -------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_CANCELLING = "cancelling"
STATUS_AWAITING_INPUT = "awaiting_input"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# A task that has reached a final state and will not transition again.
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})

# A task that still owns a worker / lock and counts as active for the UI and
# session-lock checks. ``awaiting_input`` is active-but-paused and is included
# per call site where relevant rather than baked in here, because the lock and
# liveness semantics differ (see ``check_liveness``).
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING})

# --- SSE terminal event names (wire contract with the frontend) --------------

SSE_DONE = "done"
SSE_ERROR = "error"
SSE_CANCELLED = "cancelled"
