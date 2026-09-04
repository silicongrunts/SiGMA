"""Stream hub — in-process owner of live task stream sessions.

One ``StreamSession`` per running task holds the catch-up buffer and the
subscriber fan-out for its SSE stream. The session is created before the
task starts consuming its event source, so a subscriber never waits for a
producer to appear: replay is served from the buffer immediately and live
events follow.

SSE frame format (byte-compatible wire contract with the frontend):
  ``id: {seq}\\nevent: {type}\\ndata: {json}\\n\\n``
with ``seq`` monotonically increasing from 1 per session. Silent periods
carry ``: keepalive`` comment frames so proxies and clients can tell an
idle-but-alive stream from a dead one. The per-subscriber control frames —
the idle-timeout ``error`` and the stale-cursor ``gap`` — carry no ``id``
line and are never buffered. ``gap`` (``data: {"reason": "evicted_range"}``)
precedes a replay whose cursor predates the oldest buffered event: it tells
the client its live view has a hole the buffer can no longer fill, so it
can reload the authoritative state instead of silently appending a partial
stream.

The hub's session map is process-local coordination state, not the durable
source of truth: task status lives in the per-project ``task_state`` table.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Optional

from app.core.logging import get_logger
from app.core.task_status import SSE_ERROR, TERMINAL_EVENT_TYPES

logger = get_logger(__name__)


def _control_frame(event_type: str, data: dict) -> str:
    """Frame a per-subscriber control event (no ``id:`` line, not buffered)."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class _Subscriber:
    """One SSE connection's delivery queue and overflow state.

    ``overflowed`` is set by ``push`` the first time this subscriber's queue
    rejects a frame; from then on no further frames are enqueued and the
    subscription ends itself on its next wake-up, leaving the client to
    reconnect with its last-seen cursor.
    """

    def __init__(self, maxsize: int):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False


class StreamSession:
    """Buffered, fan-out SSE stream for one task.

    The producer pushes ``{"type", "data"}`` event dicts; frames are built
    here so every subscriber sees identical, id-addressable chunks. The
    buffer's ``id:`` line makes each chunk addressable: a reconnecting
    subscriber reports the highest id it received and is replayed only the
    events after it.
    """

    BUFFER_LIMIT = 2000                    # id-framed chunks retained for catch-up
    SUBSCRIBER_QUEUE_MAXSIZE = 512         # per-subscriber pending-frame bound
    KEEPALIVE_INTERVAL_SECONDS = 5.0       # silence before a keepalive comment

    def __init__(self, task_id: str, project_id: Optional[str] = None):
        self.task_id = task_id
        self.project_id = project_id
        self.cancel_event = asyncio.Event()
        self._buffer: list[tuple[int, str]] = []     # (seq, framed chunk)
        self._subscribers: list[_Subscriber] = []    # one entry per SSE connection
        self._next_seq = 0
        self._terminal_seq: Optional[int] = None     # seq of the terminal event
        self._finished = False

    def push(self, event: dict) -> None:
        """Frame one ``{"type", "data"}`` event, buffer it, fan it out.

        The ``data`` payload is serialized verbatim, so optional keys (usage
        on error events, for example) reach subscribers untouched. When the
        buffer exceeds ``BUFFER_LIMIT`` the oldest chunks are evicted.

        The terminal event closes the stream: once one is pushed, further
        pushes are ignored, because a frame after the terminal is unreachable
        for live subscribers and contradictory on replay. A subscriber whose
        queue is full is marked overflowed and stops receiving frames; it
        ends its own stream, and the client reconnects with its last-seen
        cursor to recover the gap from the buffered copy.
        """
        if self._terminal_seq is not None:
            logger.debug(
                "Ignoring push of %r after terminal frame for task %s",
                event["type"], self.task_id,
            )
            return
        event_type = event["type"]
        seq = self._next_seq + 1
        self._next_seq = seq
        framed = (
            f"id: {seq}\n"
            f"event: {event_type}\n"
            f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        )
        self._buffer.append((seq, framed))
        if len(self._buffer) > self.BUFFER_LIMIT:
            self._buffer = self._buffer[-self.BUFFER_LIMIT:]
        if event_type in TERMINAL_EVENT_TYPES:
            self._terminal_seq = seq
        for sub in self._subscribers:
            if sub.overflowed:
                continue
            try:
                sub.queue.put_nowait((seq, framed))
            except asyncio.QueueFull:
                sub.overflowed = True
                logger.warning(
                    "A subscriber of task %s overflowed at seq %d; "
                    "its stream will end so the client can reconnect",
                    self.task_id, seq,
                )

    async def subscribe(
        self,
        *,
        cursor: Optional[int] = None,
        idle_timeout: Optional[float] = None,
    ) -> AsyncIterator[str]:
        """Yield the session's SSE frames, replaying the buffer first.

        ``cursor`` is the highest event id the subscriber has already
        received; ``None`` means a fresh subscriber that needs the whole
        buffer. Two reconnect hazards are handled explicitly:

        * A cursor predating the oldest buffered event cannot be filled from
          the buffer when eviction has already dropped events the client
          never received. Every retained event still postdates the cursor,
          so the client is replayed the whole buffer and appends it; the
          truly lost, evicted range is unrecoverable and logged, and the
          replay is preceded by a ``gap`` control frame with
          ``"reason": "evicted_range"`` so the client knows its live view
          has a hole and can reload the authoritative state.
        * A cursor ahead of the session (garbage or beyond the newest seq)
          is clamped to the session's newest seq, so it behaves as
          "everything from now on" instead of blackholing the stream.

        After the replay the subscriber receives live events. Silence longer
        than ``KEEPALIVE_INTERVAL_SECONDS`` yields a ``: keepalive`` comment
        frame; silence totalling ``idle_timeout`` seconds yields an ``error``
        frame with ``"reason": "idle_timeout"`` — a stream-health signal, not
        a task failure — and ends the stream. If a subscriber stalls long
        enough for its queue to overflow, frames stop being enqueued to it
        and its stream ends; the client reconnects with its last-seen cursor
        and the buffered copy replays the missed range. The generator also
        ends after delivering a terminal (done / error / cancelled) event, or
        once the session is finished and the buffered frames are drained.
        """
        sub = _Subscriber(self.SUBSCRIBER_QUEUE_MAXSIZE)
        stale_cursor = (
            cursor is not None
            and self._buffer
            and cursor < self._buffer[0][0] - 1
        )
        if stale_cursor:
            logger.warning(
                "Subscribe cursor %d for task %s predates the oldest buffered "
                "event %d; the evicted range is unrecoverable",
                cursor, self.task_id, self._buffer[0][0],
            )
        # Register the subscriber before snapshotting the buffer, both with no
        # await in between: a concurrent push is either already buffered
        # (captured by the snapshot) or fanned out to the queue — never lost.
        # Frames present in both are skipped in the live loop via
        # ``replay_max_seq``, so each event reaches the subscriber exactly
        # once. Replay yields directly from the snapshot rather than through
        # the queue: the queue's bound must never truncate a replay set,
        # which has no consumer until the live loop starts.
        self._subscribers.append(sub)
        snapshot = [
            (seq, framed)
            for seq, framed in self._buffer
            if cursor is None or seq > cursor
        ]
        if snapshot:
            replay_max_seq = snapshot[-1][0]
        else:
            # A forward/garbage cursor must behave as "everything from now
            # on", never skip every real frame.
            replay_max_seq = min(cursor or 0, self._next_seq)
        try:
            if stale_cursor:
                # Eviction dropped events the client never received: the gap
                # frame tells the client its live view has a hole the replay
                # cannot fill, and where the replay ends — every retained
                # event up to ``replay_until`` is stale relative to the
                # authoritative state the client reloads, so it must not
                # append them on top of that state; live events after it are
                # safe to append. The replay itself stays exactly-once per
                # seq for clients that do not reload.
                yield _control_frame("gap", {
                    "reason": "evicted_range",
                    "replay_until": replay_max_seq,
                })
            idle = 0.0
            for seq, framed in snapshot:
                yield framed
                if seq == self._terminal_seq:
                    return
            while True:
                if sub.overflowed:
                    # Frames from here on were dropped for this subscriber:
                    # end the stream so the client reconnects with its
                    # last-seen cursor and replays the gap from the buffer.
                    return
                try:
                    item = await asyncio.wait_for(
                        sub.queue.get(), timeout=self.KEEPALIVE_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    if self._finished and sub.queue.empty():
                        return
                    idle += self.KEEPALIVE_INTERVAL_SECONDS
                    yield ": keepalive\n\n"
                    if idle_timeout is not None and idle >= idle_timeout:
                        yield _control_frame(SSE_ERROR, {
                            "error": "Stream idle timeout; the task may still be running",
                            "reason": "idle_timeout",
                        })
                        return
                    continue
                if item is None:
                    # finish() wakes every subscriber once its frames are drained.
                    return
                seq, framed = item
                if seq <= replay_max_seq:
                    # Already yielded from the replay snapshot.
                    continue
                idle = 0.0
                yield framed
                if seq == self._terminal_seq:
                    return
        finally:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass

    def finish(self) -> None:
        """Mark the session ended and wake every subscriber.

        Idempotent. Frames pushed before ``finish`` were fanned out ahead of
        the wake-up, so subscribers drain their pending frames — including
        the terminal one — before ending; a subscriber that joins after
        ``finish`` replays the buffer and then ends on its next idle tick.
        An overflowed subscriber ends through its overflow flag instead of
        the wake-up.
        """
        if self._finished:
            return
        self._finished = True
        for sub in self._subscribers:
            if sub.overflowed:
                continue
            try:
                sub.queue.put_nowait(None)
            except asyncio.QueueFull:
                # The queue hit its bound before any push failed, so no
                # overflow was recorded; the subscriber drains the pending
                # frames and ends on its next idle tick via the finished flag.
                pass


class StreamHub:
    """Registry of active ``StreamSession`` objects keyed by task id."""

    def __init__(self):
        self._sessions: dict[str, StreamSession] = {}

    def create(self, task_id: str, project_id: Optional[str] = None) -> StreamSession:
        """Register a fresh session for ``task_id`` and return it.

        Callers mint a fresh task id per task, so an id collision means a
        duplicate launch: the new session replaces the mapped one and the
        collision is logged. Subscribers of the replaced session keep their
        object; only hub-level lookup (new subscribers, cancellation) moves
        to the new session.
        """
        if task_id in self._sessions:
            logger.warning(
                "A second session was created for task %s; the previous "
                "session is replaced", task_id,
            )
        session = StreamSession(task_id, project_id)
        self._sessions[task_id] = session
        return session

    def get(self, task_id: str) -> Optional[StreamSession]:
        """Return the session for ``task_id``, or None when unknown."""
        return self._sessions.get(task_id)

    def remove(self, task_id: str, session: StreamSession) -> None:
        """Detach ``task_id``'s session from the hub.

        Only the mapped session itself is popped: a caller holding a session
        that was replaced can never detach its replacement, so the replaced
        runner's cleanup cannot orphan the live session's lookup. Subscribers
        that already hold a session object keep consuming it; only hub-level
        lookup (new subscribers, cancellation) goes away.
        """
        if self._sessions.get(task_id) is session:
            del self._sessions[task_id]

    def cancel_task(self, task_id: str) -> bool:
        """Signal cooperative cancellation to the task's session.

        Returns True when an active session exists, False when the task has
        none (already finished or never launched here).
        """
        session = self._sessions.get(task_id)
        if session is None:
            return False
        session.cancel_event.set()
        return True

    def cancel_project(self, project_id: str) -> int:
        """Signal cancellation to every active session of a project.

        Returns the number of sessions signalled. Each task's runner winds
        down, finalizes its own state, and detaches its session.
        """
        task_ids = [
            task_id
            for task_id, session in self._sessions.items()
            if session.project_id == project_id
        ]
        for task_id in task_ids:
            self._sessions[task_id].cancel_event.set()
        return len(task_ids)


stream_hub = StreamHub()
