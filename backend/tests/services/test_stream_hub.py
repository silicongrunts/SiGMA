"""StreamSession / StreamHub contract tests.

The wire format asserted here (``id``/``event``/``data`` frames, keepalive
comments, terminal-event endings) is byte-compatible with what the frontend
SSE parser expects, so the assertions are exact, not substring-based.
"""

import asyncio
import json

import pytest

from app.services.stream_hub import StreamHub, StreamSession


@pytest.fixture(autouse=True)
def fast_keepalive(monkeypatch):
    """Shrink the idle tick so silence-based tests run in milliseconds."""
    monkeypatch.setattr(StreamSession, "KEEPALIVE_INTERVAL_SECONDS", 0.02)


def _event_id(frame: str) -> int:
    assert frame.startswith("id: "), f"frame missing id line: {frame!r}"
    return int(frame.split("\n", 1)[0][len("id: "):])


def _event_type(frame: str) -> str:
    for line in frame.splitlines():
        if line.startswith("event: "):
            return line[len("event: "):]
    raise AssertionError(f"frame missing event line: {frame!r}")


def _event_data(frame: str):
    for line in frame.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"frame missing data line: {frame!r}")


async def _collect(session, *, cursor=None, idle_timeout=None, stop=None):
    """Drain a subscription until it ends or ``stop(frames)`` returns True."""
    frames = []
    agen = session.subscribe(cursor=cursor, idle_timeout=idle_timeout)
    async for frame in agen:
        frames.append(frame)
        if stop is not None and stop(frames):
            break
    await agen.aclose()
    return frames


# ---------------------------------------------------------------------------
# Frame format and ordering
# ---------------------------------------------------------------------------


async def test_pushed_events_get_exact_sse_frame_format():
    session = StreamSession("task-1", "project-1")
    session.push({"type": "delta", "data": {"content": "héllo"}})
    session.push({"type": "done", "data": {}})

    frames = await _collect(session)

    assert frames == [
        'id: 1\nevent: delta\ndata: {"content": "héllo"}\n\n',
        'id: 2\nevent: done\ndata: {}\n\n',
    ]


async def test_sequence_numbers_start_at_one_and_increase_monotonically():
    session = StreamSession("task-1", "project-1")
    for index in range(5):
        session.push({"type": "delta", "data": {"content": str(index)}})

    frames = await _collect(session, stop=lambda frames: len(frames) == 5)

    assert [_event_id(f) for f in frames] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("terminal_type,payload", [
    ("done", {}),
    ("error", {"error": "provider dropped"}),
    ("cancelled", {"message": "Task cancelled by user"}),
])
async def test_terminal_event_ends_the_subscription(terminal_type, payload):
    session = StreamSession("task-1", "project-1")
    session.push({"type": "delta", "data": {"content": "x"}})
    session.push({"type": terminal_type, "data": payload})

    frames = await _collect(session)

    assert len(frames) == 2
    assert _event_type(frames[-1]) == terminal_type


async def test_error_event_payload_reaches_subscribers_verbatim():
    """Optional payload keys (usage on error events) must survive framing."""
    session = StreamSession("task-1", "project-1")
    payload = {
        "error": "provider dropped",
        "usage": {"input": 400, "output": 40, "cached": 150},
    }
    session.push({"type": "error", "data": payload})

    frames = await _collect(session)

    assert len(frames) == 1
    assert _event_data(frames[0]) == payload


async def test_pushes_after_a_terminal_event_are_ignored():
    """The terminal frame closes the stream: later terminal and non-terminal
    pushes are dropped, so a second terminal can never mask the first and
    replay stays consistent with what live subscribers saw."""
    session = StreamSession("task-1", "project-1")
    session.push({"type": "delta", "data": {"n": 0}})
    session.push({"type": "done", "data": {}})
    session.push({"type": "delta", "data": {"n": 1}})
    session.push({"type": "error", "data": {"error": "late"}})

    frames = await _collect(session)

    assert [_event_type(f) for f in frames] == ["delta", "done"]


async def test_every_buffered_frame_is_stamped_with_its_integer_id():
    """Cursor replay addressing depends on every buffered event carrying its
    ``id:`` line from push time on: each buffered frame must start with the
    exact ``id: {seq}`` prefix of the seq it is stored under."""
    session = StreamSession("task-1", "project-1")
    for index in range(5):
        session.push({"type": "delta", "data": {"n": index}})
    session.push({"type": "done", "data": {}})

    assert [seq for seq, _ in session._buffer] == [1, 2, 3, 4, 5, 6]
    for seq, framed in session._buffer:
        assert framed.startswith(f"id: {seq}\nevent: ")
        assert _event_id(framed) == seq


# ---------------------------------------------------------------------------
# Cursor replay semantics
# ---------------------------------------------------------------------------


async def test_cursor_none_replays_whole_buffer():
    session = StreamSession("task-1", "project-1")
    for index in range(3):
        session.push({"type": "delta", "data": {"n": index}})

    frames = await _collect(session, cursor=None, stop=lambda frames: len(frames) == 3)

    assert [_event_data(f)["n"] for f in frames] == [0, 1, 2]


async def test_cursor_replays_only_events_after_cursor():
    session = StreamSession("task-1", "project-1")
    for index in range(3):
        session.push({"type": "delta", "data": {"n": index}})

    frames = await _collect(session, cursor=2, stop=lambda frames: len(frames) == 1)

    assert [_event_data(f)["n"] for f in frames] == [2]


async def test_cursor_at_oldest_buffered_event_replays_the_rest(monkeypatch):
    monkeypatch.setattr(StreamSession, "BUFFER_LIMIT", 2)
    session = StreamSession("task-1", "project-1")
    for index in range(3):
        session.push({"type": "delta", "data": {"n": index}})  # buffer holds seqs 2, 3

    frames = await _collect(session, cursor=2, stop=lambda frames: len(frames) == 1)

    assert [_event_data(f)["n"] for f in frames] == [2]


async def test_stale_cursor_replays_every_retained_event_exactly_once(monkeypatch):
    """A cursor predating the oldest retained event is replayed from the
    buffer head: every retained event postdates the cursor, so the client
    appends the whole retained buffer exactly once, with no duplicates. This
    cursor is the seamless boundary (the client has everything up to the
    frame before the buffer head), so no gap frame precedes the replay."""
    monkeypatch.setattr(StreamSession, "BUFFER_LIMIT", 2)
    session = StreamSession("task-1", "project-1")
    for index in range(3):
        session.push({"type": "delta", "data": {"n": index}})  # buffer holds seqs 2, 3

    frames = await _collect(session, cursor=1, stop=lambda frames: len(frames) == 2)

    assert [_event_id(f) for f in frames] == [2, 3]
    assert [_event_data(f)["n"] for f in frames] == [1, 2]


async def test_cursor_predating_received_events_emits_gap_frame_then_replays(
    monkeypatch,
):
    """A cursor below the seamless boundary means eviction dropped events the
    client never received: exactly one unaddressed ``gap`` control frame
    precedes the replay, which still delivers every retained event exactly
    once."""
    monkeypatch.setattr(StreamSession, "BUFFER_LIMIT", 2)
    session = StreamSession("task-1", "project-1")
    for index in range(4):
        session.push({"type": "delta", "data": {"n": index}})  # buffer holds seqs 3, 4

    frames = await _collect(session, cursor=1, stop=lambda frames: len(frames) == 3)

    assert frames[0] == (
        'event: gap\ndata: {"reason": "evicted_range", "replay_until": 4}\n\n'
    )
    assert [_event_id(f) for f in frames[1:]] == [3, 4]
    assert [_event_data(f)["n"] for f in frames[1:]] == [2, 3]


async def test_gap_frame_is_per_subscriber_and_never_buffered(monkeypatch):
    """The gap frame exists only on the stale subscriber's stream: a fresh
    subscriber to the same session sees no gap frame, and the buffer holds
    only id-addressed event chunks."""
    monkeypatch.setattr(StreamSession, "BUFFER_LIMIT", 2)
    session = StreamSession("task-1", "project-1")
    for index in range(4):
        session.push({"type": "delta", "data": {"n": index}})

    stale_frames = await _collect(
        session, cursor=1, stop=lambda frames: len(frames) == 3,
    )
    fresh_frames = await _collect(
        session, cursor=None, stop=lambda frames: len(frames) == 2,
    )

    assert stale_frames[0].startswith("event: gap\n")
    assert all(f.startswith("id: ") for f in fresh_frames)
    assert all(framed.startswith("id: ") for _, framed in session._buffer)


async def test_cursor_one_before_the_first_seq_replays_the_whole_stream():
    """A cursor of 0 against a buffer starting at seq 1 is the seamless
    boundary: a plain full replay the client appends without duplicates."""
    session = StreamSession("task-1", "project-1")
    for index in range(3):
        session.push({"type": "delta", "data": {"n": index}})

    frames = await _collect(session, cursor=0, stop=lambda frames: len(frames) == 3)

    assert [_event_id(f) for f in frames] == [1, 2, 3]
    assert [_event_data(f)["n"] for f in frames] == [0, 1, 2]


async def test_cursor_beyond_session_delivers_live_events_immediately():
    """A garbage/forward cursor is clamped to the session's newest seq: the
    subscriber gets everything from now on instead of a blackholed stream."""
    session = StreamSession("task-1", "project-1")
    session.push({"type": "delta", "data": {"n": 0}})
    session.push({"type": "delta", "data": {"n": 1}})

    agen = session.subscribe(cursor=10**9)
    pending = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.01)  # the subscription is live and waiting
    session.push({"type": "delta", "data": {"n": 2}})

    frame = await asyncio.wait_for(pending, timeout=1)
    assert _event_id(frame) == 3
    assert _event_data(frame)["n"] == 2
    await agen.aclose()


async def test_buffer_evicts_oldest_frames_beyond_limit(monkeypatch):
    monkeypatch.setattr(StreamSession, "BUFFER_LIMIT", 3)
    session = StreamSession("task-1", "project-1")
    for index in range(5):
        session.push({"type": "delta", "data": {"n": index}})

    frames = await _collect(session, cursor=None, stop=lambda frames: len(frames) == 3)

    assert [_event_data(f)["n"] for f in frames] == [2, 3, 4]


async def test_replay_larger_than_queue_maxsize_delivers_every_frame():
    """A replay set exceeding the subscriber queue bound must not be
    truncated: the fresh subscriber receives every buffered frame, ending
    with the terminal one."""
    session = StreamSession("task-1", "project-1")
    total = StreamSession.SUBSCRIBER_QUEUE_MAXSIZE + 89
    for index in range(total - 1):
        session.push({"type": "delta", "data": {"n": index}})
    session.push({"type": "done", "data": {}})

    frames = await _collect(session)

    assert [_event_id(f) for f in frames] == list(range(1, total + 1))
    assert _event_type(frames[-1]) == "done"


async def test_delta_replay_larger_than_queue_maxsize_delivers_every_frame():
    """The same no-truncation guarantee applies to a reconnecting subscriber
    whose delta set after its cursor exceeds the queue bound."""
    session = StreamSession("task-1", "project-1")
    total = StreamSession.SUBSCRIBER_QUEUE_MAXSIZE + 89
    for index in range(total):
        session.push({"type": "delta", "data": {"n": index}})
    session.push({"type": "done", "data": {}})

    frames = await _collect(session, cursor=5)

    assert [_event_id(f) for f in frames] == list(range(6, total + 2))
    assert _event_type(frames[-1]) == "done"


async def test_pushes_during_replay_are_delivered_exactly_once():
    """Events pushed while a subscriber is mid-replay arrive exactly once:
    none duplicated between snapshot and queue, none lost."""
    session = StreamSession("task-1", "project-1")
    replay_count = StreamSession.SUBSCRIBER_QUEUE_MAXSIZE + 10
    for index in range(replay_count):
        session.push({"type": "delta", "data": {"n": index}})

    async def mid_replay_push():
        for index in range(5):
            await asyncio.sleep(0)  # land pushes between replayed frames
            session.push({"type": "delta", "data": {"n": replay_count + index}})
        session.push({"type": "done", "data": {}})

    pusher = asyncio.create_task(mid_replay_push())
    frames = []
    agen = session.subscribe()
    async for frame in agen:
        frames.append(frame)
        await asyncio.sleep(0)  # let the pusher interleave with the replay
    await pusher

    # ids 1..replay_count from the replay, replay_count+1..+5 live, then done.
    assert [_event_id(f) for f in frames] == list(range(1, replay_count + 7))
    assert _event_type(frames[-1]) == "done"


# ---------------------------------------------------------------------------
# Subscriber queue overflow
# ---------------------------------------------------------------------------


async def _subscribe_and_stall(session):
    """Register a subscriber, deliver its first live frame, then stall it
    while more events are pushed so its queue overflows."""
    agen = session.subscribe()
    pending = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.01)  # let the subscription register and start waiting
    session.push({"type": "delta", "data": {"n": 0}})
    assert _event_data(await pending)["n"] == 0
    session.push({"type": "delta", "data": {"n": 1}})
    session.push({"type": "delta", "data": {"n": 2}})  # queue full: overflow
    return agen


async def test_overflow_ends_the_stalled_stream_cleanly(monkeypatch):
    """A subscriber whose queue fills is marked overflowed: frames stop being
    enqueued to it and its stream ends with no further frames — no control
    frame, no hang. The client reconnects with its last-seen cursor and the
    buffered copy replays the missed range."""
    monkeypatch.setattr(StreamSession, "SUBSCRIBER_QUEUE_MAXSIZE", 1)
    session = StreamSession("task-1", "project-1")
    agen = await _subscribe_and_stall(session)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), timeout=1)
    await agen.aclose()


async def test_overflow_does_not_disturb_other_subscribers(monkeypatch):
    """Only the stalled subscriber is cut over to the reconnect signal; an
    actively reading subscriber keeps receiving every event."""
    monkeypatch.setattr(StreamSession, "SUBSCRIBER_QUEUE_MAXSIZE", 1)
    session = StreamSession("task-1", "project-1")
    await _subscribe_and_stall(session)

    async def push_rest():
        for n in (10, 11):
            await asyncio.sleep(0.01)  # let the live subscriber drain between pushes
            session.push({"type": "delta", "data": {"n": n}})
        await asyncio.sleep(0.01)
        session.push({"type": "done", "data": {}})

    pusher = asyncio.create_task(push_rest())
    frames = []
    agen = session.subscribe()
    async for frame in agen:
        frames.append(frame)
        await asyncio.sleep(0)
    await pusher

    # The live subscriber replays the buffered events and then receives every
    # live event — including those the stalled subscriber missed.
    assert [_event_data(f)["n"] for f in frames if _event_type(f) == "delta"] == [
        0, 1, 2, 10, 11,
    ]
    assert _event_type(frames[-1]) == "done"
    await agen.aclose()


async def test_reconnect_after_overflow_recovers_missed_events_exactly_once(
    monkeypatch,
):
    """After an overflow ends the stream cleanly, reconnecting with the
    last-seen cursor replays exactly the events the subscriber never
    received — no duplicates of what it has, no loss of what it missed."""
    monkeypatch.setattr(StreamSession, "SUBSCRIBER_QUEUE_MAXSIZE", 1)
    session = StreamSession("task-1", "project-1")
    agen = await _subscribe_and_stall(session)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), timeout=1)
    await agen.aclose()
    session.push({"type": "delta", "data": {"n": 3}})
    session.push({"type": "done", "data": {}})

    monkeypatch.setattr(StreamSession, "SUBSCRIBER_QUEUE_MAXSIZE", 512)
    frames = await _collect(session, cursor=1, stop=lambda frames: len(frames) == 4)

    assert [_event_id(f) for f in frames] == [2, 3, 4, 5]
    assert [_event_data(f)["n"] for f in frames[:3]] == [1, 2, 3]
    assert _event_type(frames[-1]) == "done"


async def test_finish_with_a_full_queue_terminates_the_subscriber_after_drain(
    monkeypatch,
):
    """finish() landing while a subscriber's queue is exactly full still ends
    that subscriber: the wake-up sentinel cannot be enqueued, so the drained
    queue plus the finished flag end the stream on the next idle tick."""
    monkeypatch.setattr(StreamSession, "SUBSCRIBER_QUEUE_MAXSIZE", 2)
    session = StreamSession("task-1", "project-1")
    agen = session.subscribe()
    pending = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.01)  # let the subscription register and start waiting
    session.push({"type": "delta", "data": {"n": 0}})
    assert _event_data(await pending)["n"] == 0
    session.push({"type": "delta", "data": {"n": 1}})
    session.push({"type": "delta", "data": {"n": 2}})  # queue full, not overflowed
    session.finish()

    frames = []
    with pytest.raises(StopAsyncIteration):
        while True:
            frames.append(await asyncio.wait_for(agen.__anext__(), timeout=1))
    await agen.aclose()

    assert [_event_data(f)["n"] for f in frames] == [1, 2]


# ---------------------------------------------------------------------------
# Silence handling: keepalives and idle timeout
# ---------------------------------------------------------------------------


async def test_silent_periods_emit_keepalive_comment_frames():
    session = StreamSession("task-1", "project-1")

    frames = await _collect(session, stop=lambda frames: len(frames) == 2)

    assert frames[0] == ": keepalive\n\n"
    assert frames[1] == ": keepalive\n\n"


async def test_idle_timeout_ends_stream_with_reconnectable_signal():
    session = StreamSession("task-1", "project-1")

    frames = await _collect(session, idle_timeout=0.05)

    assert all(f == ": keepalive\n\n" for f in frames[:-1])
    assert _event_type(frames[-1]) == "error"
    assert _event_data(frames[-1]) == {
        "error": "Stream idle timeout; the task may still be running",
        "reason": "idle_timeout",
    }


async def test_activity_resets_the_idle_clock():
    """Frames arriving between keepalive ticks prevent the idle timeout.

    Margins stay real-clock but generous under suite load: the activity gap
    (0.05s) is well above the keepalive tick (0.02s) and the idle timeout
    (0.3s) leaves a >=6x margin over the gap, so an 80ms scheduler stall
    cannot trip the timeout and flip the assertion."""
    session = StreamSession("task-1", "project-1")

    async def slow_push():
        for index in range(3):
            await asyncio.sleep(0.05)
            session.push({"type": "delta", "data": {"n": index}})
        session.push({"type": "done", "data": {}})

    pusher = asyncio.create_task(slow_push())
    frames = await _collect(session, idle_timeout=0.3)
    await pusher

    types = [
        _event_type(f) if f.startswith("id: ") else "keepalive" for f in frames
    ]
    assert types[-1] == "done"
    assert types.count("delta") == 3
    assert "error" not in types


# ---------------------------------------------------------------------------
# finish() semantics
# ---------------------------------------------------------------------------


async def test_finish_ends_subscribers_after_pending_frames_drain():
    session = StreamSession("task-1", "project-1")
    session.push({"type": "delta", "data": {"n": 1}})
    session.push({"type": "delta", "data": {"n": 2}})
    session.finish()

    frames = await _collect(session)

    assert [_event_data(f)["n"] for f in frames] == [1, 2]


async def test_subscriber_joining_after_finish_replays_buffer_then_ends():
    session = StreamSession("task-1", "project-1")
    session.push({"type": "delta", "data": {"n": 1}})
    session.finish()

    frames = await _collect(session)

    assert len(frames) == 1
    assert _event_data(frames[0])["n"] == 1


# ---------------------------------------------------------------------------
# StreamHub
# ---------------------------------------------------------------------------


def test_hub_cancel_task_signals_session_or_reports_missing():
    hub = StreamHub()
    assert hub.cancel_task("missing") is False

    session = hub.create("task-1", "project-1")
    assert hub.cancel_task("task-1") is True
    assert session.cancel_event.is_set()


def test_hub_create_replaces_existing_session_for_the_same_id():
    """Callers mint fresh task ids, so a duplicate create is a mis-launch:
    the new session replaces the mapped one and hub-level targeting moves to
    it, while the replaced session object keeps serving its subscribers."""
    hub = StreamHub()
    replaced = hub.create("task-1", "project-1")
    replacement = hub.create("task-1", "project-1")

    assert hub.get("task-1") is replacement
    assert hub.cancel_task("task-1") is True
    assert replacement.cancel_event.is_set()
    assert not replaced.cancel_event.is_set()


def test_hub_remove_only_detaches_the_mapped_session():
    """remove verifies identity: the replaced session's runner cannot detach
    its replacement, so the live session's lookup survives an orphan's
    cleanup."""
    hub = StreamHub()
    replaced = hub.create("task-1", "project-1")
    replacement = hub.create("task-1", "project-1")

    hub.remove("task-1", replaced)
    assert hub.get("task-1") is replacement

    hub.remove("task-1", replacement)
    assert hub.get("task-1") is None


def test_hub_cancel_project_signals_only_that_projects_sessions():
    hub = StreamHub()
    session_p1 = hub.create("task-1", "project-1")
    session_p2 = hub.create("task-2", "project-2")

    assert hub.cancel_project("project-1") == 1

    assert session_p1.cancel_event.is_set()
    assert not session_p2.cancel_event.is_set()


async def test_hub_remove_detaches_lookup_but_held_sessions_keep_streaming():
    hub = StreamHub()
    session = hub.create("task-1", "project-1")
    collector = session.subscribe()
    pending = asyncio.create_task(collector.__anext__())
    await asyncio.sleep(0.01)  # let the subscription register and start waiting

    hub.remove("task-1", session)

    assert hub.get("task-1") is None
    session.push({"type": "delta", "data": {"n": 1}})
    frame = await pending
    assert _event_data(frame)["n"] == 1
    await collector.aclose()
