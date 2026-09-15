"""Partial-assistant checkpointing during streamed turns.

Drives the REAL QueryLoop → LLMLoopRunner chain against a real per-project
SQLite database; only the LLM stream is scripted (with real inter-chunk
delays so the time-based checkpoint fires mid-stream). Each test pins one
crash-window contract: the partial assistant text must be durably persisted
while it grows, must finalize into exactly one row, and must integrate with
the round-level history model on the next turn.
"""

import asyncio
import contextlib
from importlib import import_module

import pytest
import pytest_asyncio

import_module("app.agents.tools")
import app.services.llm_loop_runner as llm_loop_runner_module
import app.services.query_loop as query_loop_module
from app.core.utils import generate_id
from app.database.repos.message_repo import MessageRepository
from app.database.unit_of_work import UnitOfWork
from app.services.llm_loop_runner import LLMLoopRunner
from app.services.llm_service import with_complete_tool_results
from app.services.query_loop import QueryLoop
from tests.ai.matrix_harness import make_turn

USAGE = {"prompt_tokens": 100, "completion_tokens": 20}

# Test cadence: flush as soon as a few chunks arrived, so tests exercise the
# mid-stream flushes without waiting for the production 2s interval.
CHECKPOINT_MIN_CHARS = 10
CHUNK_DELAY = 0.03


@pytest_asyncio.fixture
async def fast_checkpoint(monkeypatch):
    monkeypatch.setattr(
        llm_loop_runner_module, "PARTIAL_CHECKPOINT_MIN_CHARS", CHECKPOINT_MIN_CHARS,
    )
    monkeypatch.setattr(
        llm_loop_runner_module, "PARTIAL_CHECKPOINT_INTERVAL_SECONDS", 0.0,
    )


async def assistant_contents(project, session_id):
    async with UnitOfWork(project) as uow:
        rows = await uow.messages.get_messages(session_id)
    return [r.content for r in rows if r.role == "assistant"]


def script_stream(monkeypatch, chunks, *, chunk_delay=CHUNK_DELAY, observer=None,
                  after_chunks=None):
    """Script ``_stream_llm`` as a real delta stream.

    Each chunk is queued as a delta with a delay so the runner's consume loop
    processes them over time. ``observer`` runs after each chunk is queued
    (used to snapshot the DB mid-stream); ``after_chunks`` runs once all
    chunks were queued and may raise to simulate a stream failure. Returns
    the full text the stream completes with ("" when it fails).
    """
    calls = {"messages": []}

    async def fake_stream_llm(ctx, messages, delta_queue):
        calls["messages"].append(list(messages))
        for chunk in chunks:
            await delta_queue.put(("delta", chunk))
            if observer:
                await observer()
            await asyncio.sleep(chunk_delay)
        if after_chunks:
            await after_chunks()
        return ("".join(chunks), "", [], USAGE)

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm))
    return calls


async def collect(loop):
    return [event async for event in loop.run()]


@pytest.mark.asyncio
async def test_long_streamed_turn_checkpoints_partial_and_finalizes_single_row(
    project, fast_checkpoint, monkeypatch,
):
    """Mid-stream the DB holds ONE partial assistant row whose content grows
    with the stream; after the turn completes there is exactly one final row
    with the complete text — no checkpoint duplicate."""
    chunks = ["Hello ", "wonderful ", "streamed ", "world ", "of ", "text!"]
    full_text = "".join(chunks)

    session_id, task_id = await make_turn(project)
    snapshots = []

    async def observer():
        snapshots.append(await assistant_contents(project, session_id))

    script_stream(monkeypatch, chunks, observer=observer)
    events = await collect(QueryLoop(project, session_id, task_id=task_id))

    assert events[-1]["type"] == "done"
    assert "error" not in [e["type"] for e in events]

    # At least two mid-stream observations, showing one partial row whose
    # content is a growing prefix of the final text.
    seen_contents = [s[0] for s in snapshots if s]
    assert len(seen_contents) >= 2
    assert all(full_text.startswith(c) for c in seen_contents)
    assert seen_contents[-1].startswith(seen_contents[0])
    assert len(seen_contents[-1]) > len(seen_contents[0])

    final = await assistant_contents(project, session_id)
    assert final == [full_text]


@pytest.mark.asyncio
async def test_cancel_mid_round_persists_partial_text(project, fast_checkpoint, monkeypatch):
    """A user stop during streaming persists the partial assistant text:
    after a refresh the interrupted turn is still in history, exactly once."""
    chunks = ["alpha ", "beta ", "gamma ", "delta "]
    cancel_event = asyncio.Event()
    session_id, task_id = await make_turn(project)

    async def fake_stream_llm(ctx, messages, delta_queue):
        await delta_queue.put(("delta", chunks[0]))
        await asyncio.sleep(CHUNK_DELAY)
        cancel_event.set()
        await asyncio.sleep(CHUNK_DELAY)
        for chunk in chunks[1:]:
            await delta_queue.put(("delta", chunk))
            await asyncio.sleep(CHUNK_DELAY)
        return ("".join(chunks), "", [], USAGE)

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm))
    events = await collect(
        QueryLoop(project, session_id, task_id=task_id, cancel_event=cancel_event),
    )

    types = [e["type"] for e in events]
    assert types[-1] == "cancelled"
    assert "done" not in types and "error" not in types

    contents = await assistant_contents(project, session_id)
    # Exactly one assistant row carrying the text streamed before the stop
    # (the runner may stop after the first or the second chunk).
    assert len(contents) == 1
    assert contents[0]
    assert "".join(chunks).startswith(contents[0])


@pytest.mark.asyncio
async def test_crash_mid_round_keeps_checkpoint_row_for_next_turn(
    project, fast_checkpoint, monkeypatch,
):
    """Hard-kill the runner mid-round: the committed checkpoint row survives
    with nothing further appended, and a restart-style next submit sees the
    partial text in its history (exactly one row, outbound repair untouched)."""
    chunks = ["crash ", "test ", "partial ", "text ", "kept ", "safely"]
    session_id, task_id = await make_turn(project)
    script_stream(monkeypatch, chunks)

    events = []
    runner_task = asyncio.create_task(
        collect_events(QueryLoop(project, session_id, task_id=task_id), events),
    )

    # Wait until the checkpoint row is durably committed, then kill the task.
    partial = None
    for _ in range(500):
        contents = await assistant_contents(project, session_id)
        if contents:
            partial = contents[0]
            break
        await asyncio.sleep(0.01)
    assert partial, "checkpoint row never committed mid-stream"

    runner_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner_task
    # Let the orphaned stream task drain so nothing races the final asserts.
    await asyncio.sleep(0.4)

    # Nothing finalized after the kill: still exactly one partial row.
    contents = await assistant_contents(project, session_id)
    assert len(contents) == 1
    assert contents[0].startswith(partial)
    assert "".join(chunks).startswith(contents[0])

    # Restart-style recovery: startup reconciliation fails the killed task
    # row, then the next submit appends a user message and a fresh task.
    async with UnitOfWork(project) as uow:
        await uow.task_state.mark_failed(task_id, error="simulated crash")
        await uow.messages.create(
            session_id=session_id, role="user", content="continue please",
        )
        task_id2 = generate_id()
        await uow.task_state.set_queued(task_id2, session_id=session_id)

    calls = {"llm_history": []}

    async def fake_stream_llm(ctx, messages, delta_queue):
        calls["llm_history"].append(list(messages))
        return ("resumed reply", "", [], USAGE)

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm))
    next_events = await collect(QueryLoop(project, session_id, task_id=task_id2))

    assert next_events[-1]["type"] == "done"
    sent = calls["llm_history"][0]
    partial_entries = [m for m in sent if m.get("role") == "assistant"]
    assert len(partial_entries) == 1
    assert partial_entries[0]["content"] == contents[0]
    # Outbound repair is satisfied: nothing unpaired to synthesize.
    assert with_complete_tool_results(sent) == sent

    async with UnitOfWork(project) as uow:
        rows = await uow.messages.get_messages(session_id)
    assert [(r.role, r.content) for r in rows] == [
        ("user", "do the work"),
        ("assistant", contents[0]),
        ("user", "continue please"),
        ("assistant", "resumed reply"),
    ]


async def collect_events(loop, into):
    async for event in loop.run():
        into.append(event)


@pytest.mark.asyncio
async def test_stream_error_mid_round_persists_partial_then_error(
    project, fast_checkpoint, monkeypatch,
):
    """A provider failure mid-stream keeps the text streamed so far as its own
    assistant row, followed by the visible error row — no text lost, no
    tool-call pairing damage."""
    chunks = ["visible ", "partial ", "text ", "before ", "failure"]

    async def fail_after_chunks():
        raise ConnectionError("provider dropped mid-stream")

    session_id, task_id = await make_turn(project)
    script_stream(monkeypatch, chunks, after_chunks=fail_after_chunks)
    events = await collect(QueryLoop(project, session_id, task_id=task_id))

    types = [e["type"] for e in events]
    assert types[-1] == "error"

    async with UnitOfWork(project) as uow:
        rows = await uow.messages.get_messages(session_id)
    assert [(r.role, r.content) for r in rows] == [
        ("user", "do the work"),
        ("assistant", "".join(chunks)),
        ("assistant", "Error: provider dropped mid-stream"),
    ]


@pytest.mark.asyncio
async def test_compaction_after_checkpointed_round_keeps_history_consistent(
    project, fast_checkpoint, monkeypatch,
):
    """A checkpointed assistant round followed by passive compaction before
    the next round must not duplicate or lose messages: the checkpointed row
    finalizes once, the boundary splits history, and the next round appends
    cleanly after it."""
    chunks = ["compacted ", "round ", "one ", "streamed ", "text"]

    session_id, task_id = await make_turn(project)
    script_stream(monkeypatch, chunks)

    # First LLM call answers with a tool call (so the turn continues into a
    # second round); compaction then fires before the second round's call.
    async def fake_stream_llm(ctx, messages, delta_queue):
        calls["n"] += 1
        calls["llm_history"].append(list(messages))
        if calls["n"] == 1:
            for chunk in chunks:
                await delta_queue.put(("delta", chunk))
                await asyncio.sleep(CHUNK_DELAY)
            return ("".join(chunks), "", [{
                "id": "call_cp", "name": "read",
                "params": {"file_path": "notes.md"},
            }], USAGE)
        return ("final after compaction", "", [], USAGE)

    calls = {"n": 0, "llm_history": []}
    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(fake_stream_llm))

    real_stats = query_loop_module.compaction_service.stats_for_messages_incremental

    def stats_with_second_call_over_threshold(*args, **kwargs):
        stats = real_stats(*args, **kwargs)
        if calls["n"] >= 1:
            return stats.__class__(
                current_tokens=stats.compact_threshold + 1,
                compact_threshold=stats.compact_threshold,
                max_context_length=stats.max_context_length,
            )
        return stats

    monkeypatch.setattr(
        query_loop_module.compaction_service,
        "stats_for_messages_incremental",
        stats_with_second_call_over_threshold,
    )

    from app.services.compaction_service import CompactionResult, ContextStats

    async def fake_compact(messages, **kwargs):
        # Mirror _build_compacted_messages: the rebuilt list keeps the outer
        # system prompt at index 0 and presents the boundary as a user
        # message (provider contract), never as system.
        system_prompt = next(m for m in messages if m["role"] == "system")
        return CompactionResult(
            summary="summarized",
            boundary_content="[passive] summarized",
            messages=[system_prompt, {"role": "user", "content": "[passive] summarized"}],
            stats=ContextStats(current_tokens=30, compact_threshold=100, max_context_length=200),
            usage=None,
        )

    monkeypatch.setattr(
        query_loop_module.compaction_service, "compact_messages", fake_compact,
    )

    events = await collect(QueryLoop(project, session_id, task_id=task_id))

    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" not in types
    assert "compact_done" in types

    async with UnitOfWork(project) as uow:
        rows = await uow.messages.get_messages(session_id)
    assert [(r.role, r.is_boundary) for r in rows] == [
        ("user", False),
        ("assistant", False),  # exactly one row for the checkpointed round
        ("tool", False),
        ("system", True),
        ("assistant", False),
    ]
    assert rows[1].content == "".join(chunks)
    assert "call_cp" in (rows[1].tool_calls or "")
    assert rows[2].tool_call_id == "call_cp"
    assert rows[4].content == "final after compaction"
    # The second round's LLM call saw the outer system prompt and only the
    # compacted context, with the boundary as the user message the provider
    # contract requires.
    assert [
        (m["role"], m["content"]) for m in calls["llm_history"][1]
    ] == [
        ("system", calls["llm_history"][0][0]["content"]),
        ("user", "[passive] summarized"),
    ]


@pytest.mark.asyncio
async def test_finalized_checkpoint_row_is_not_reupdated_after_round(
    project, fast_checkpoint, monkeypatch,
):
    """Once a checkpointed assistant row's final content was persisted at its
    round boundary, later saves of the same turn must skip it via the
    history-count slice — not re-update it on every flush (O(n²) writes)."""
    chunks = ["finalized ", "checkpoint ", "row ", "with ", "plenty ", "of ", "text"]
    session_id, task_id = await make_turn(project)

    timeline = []
    real_update = MessageRepository.stage_update_content

    async def counting_update(self, message_id, **kwargs):
        timeline.append(("update", message_id))
        return await real_update(self, message_id, **kwargs)

    monkeypatch.setattr(MessageRepository, "stage_update_content", counting_update)

    # Round 1: a tool call so the turn continues into a second round;
    # round 2: the final answer.
    tool_calls_by_round = [
        [{"id": "call_t", "name": "read", "params": {"file_path": "notes.md"}}],
        [],
    ]
    stream_state = {"round": 0}

    async def stream_llm(ctx, messages, delta_queue):
        stream_state["round"] += 1
        round_no = stream_state["round"]
        if round_no == 2:
            # Round 2's LLM call happens after round 1's row was finalized;
            # nothing from here on may touch that row again.
            timeline.append(("round2", None))
        for chunk in chunks:
            await delta_queue.put(("delta", chunk))
            await asyncio.sleep(CHUNK_DELAY)
        return ("".join(chunks), "", tool_calls_by_round[min(round_no - 1, 1)], USAGE)

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(stream_llm))

    events = await collect(QueryLoop(project, session_id, task_id=task_id))

    types = [e["type"] for e in events]
    assert types[-1] == "done"
    assert "error" not in types

    async with UnitOfWork(project) as uow:
        rows = await uow.messages.get_messages(session_id)
    assistant_rows = [r for r in rows if r.role == "assistant"]
    assert len(assistant_rows) == 2
    checkpointed_row_id = assistant_rows[0].id

    round2_index = next(
        i for i, (kind, _) in enumerate(timeline) if kind == "round2"
    )
    updates_after_round2 = [
        mid for kind, mid in timeline[round2_index:] if kind == "update" and mid == checkpointed_row_id
    ]
    assert updates_after_round2 == []
    # The row was still refreshed while its round was streaming and at its
    # own round boundary — at least one update happened before round 2.
    updates_before_round2 = [
        mid for kind, mid in timeline[:round2_index] if kind == "update" and mid == checkpointed_row_id
    ]
    assert updates_before_round2
