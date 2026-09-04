"""Chat SSE event contract — the single definition of every event type the
LLM loop pipeline emits, plus the payload keys the frontend relies on.

``make_event`` is the only sanctioned constructor (``LLMLoopRunner.sse``
delegates to it). Unknown event types and missing required payload keys raise
at the emission site, so contract drift fails loudly in tests instead of
silently reaching the frontend.

Transport-level SSE frames written outside the loop (stream session terminal
events, ``task_id``, settings-check events) are separate protocols and
deliberately not part of this registry.
"""

from app.core.task_status import SSE_CANCELLED, SSE_DONE, SSE_ERROR

SSE_DELTA = "delta"
SSE_TOOL_START = "tool_start"
SSE_TOOL_END = "tool_end"
SSE_THOUGHT = "thought"
SSE_FILE_CHANGED = "file_changed"
SSE_ANNOTATION_CHANGED = "annotation_changed"
SSE_TASK_LIST = "task_list"
SSE_AWAITING_INPUT = "awaiting_input"
SSE_AGENT_EVENT = "agent_event"
SSE_CONTEXT_STATS = "context_stats"
SSE_COMPACT_START = "compact_start"
SSE_COMPACT_DONE = "compact_done"
SSE_TURN_USAGE = "turn_usage"
SSE_STREAM_STATUS = "stream_status"

# Event type -> payload keys that must always be present. Keys the frontend
# treats as optional (usage, tool_call_id, file_edit, ...) are not listed.
EVENT_PAYLOAD_SPECS: dict[str, tuple[str, ...]] = {
    SSE_DELTA: ("content",),
    SSE_THOUGHT: ("content",),
    SSE_TOOL_START: ("tool", "params"),
    SSE_TOOL_END: ("tool", "result_summary"),
    SSE_ERROR: ("error",),
    SSE_AWAITING_INPUT: ("interaction_type",),
    SSE_AGENT_EVENT: ("parent_tool_call_id", "inner_type"),
    SSE_FILE_CHANGED: ("paths",),
    SSE_ANNOTATION_CHANGED: ("file_path",),
    SSE_TASK_LIST: ("tasks",),
    SSE_TURN_USAGE: ("usage",),
    SSE_CANCELLED: (),
    SSE_DONE: (),
    SSE_CONTEXT_STATS: (),
    SSE_COMPACT_START: (),
    SSE_COMPACT_DONE: (),
    SSE_STREAM_STATUS: (),
}


def make_event(event_type: str, data: dict) -> dict:
    """Build one chat SSE event dict, enforcing the payload contract."""
    try:
        required = EVENT_PAYLOAD_SPECS[event_type]
    except KeyError:
        raise ValueError(f"Unknown chat SSE event type: {event_type!r}") from None
    payload = data or {}
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(
            f"Chat SSE event {event_type!r} is missing required payload keys: {missing}"
        )
    return {"type": event_type, "data": payload}
