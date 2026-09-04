"""Shared task lifecycle constants.

The task status column (``TaskState.status`` / ``BackgroundTask.status``) is a
free-form string shared across the database layer, services, and the task
runtime. The SSE terminal event names are a wire contract with the frontend.
Centralizing both here prevents silent drift: a typo such as ``"canceling"``
would otherwise break the terminal-detection short-circuits spread across
several modules without any compile-time signal.

This module holds the canonical definitions every producer and consumer
imports.
"""

# --- Task lifecycle statuses -------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_CANCELLING = "cancelling"
STATUS_AWAITING_INPUT = "awaiting_input"
STATUS_INTERACTION_CONSUMING = "interaction_consuming"
STATUS_INTERACTION_FAILED = "interaction_failed"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# A task that has reached a final state and will not transition again.
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})

# A task that still owns a runner / lock and counts as active for the UI and
# session-lock checks. ``awaiting_input`` is active-but-paused and is included
# per call site where relevant rather than baked in here, because the lock
# semantics differ for a parked interaction checkpoint.
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING})

# Interaction rows are owned by the resume protocol rather than a runner.
INTERACTION_STATUSES = frozenset({
    STATUS_AWAITING_INPUT,
    STATUS_INTERACTION_CONSUMING,
    STATUS_INTERACTION_FAILED,
})

# --- SSE terminal event names (wire contract with the frontend) --------------

SSE_DONE = "done"
SSE_ERROR = "error"
SSE_CANCELLED = "cancelled"

# Event types that end a subscriber's SSE stream once delivered.
TERMINAL_EVENT_TYPES = frozenset({SSE_DONE, SSE_ERROR, SSE_CANCELLED})
