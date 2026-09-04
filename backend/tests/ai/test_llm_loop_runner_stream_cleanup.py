"""Hard cancellation of the runner must not orphan the streaming LLM task.

When TaskRunner hard-cancels the loop runner, the CancelledError is injected
at the consume loop's await point — the separately-created ``llm_task`` is
not on that unwind path, so the runner itself is responsible for cancelling
and reaping it. Otherwise the provider request keeps streaming to natural
completion with nobody retrieving the result.
"""

import asyncio
import contextlib

import pytest

from app.services.llm_loop_runner import LLMLoopRunner, LoopContext


@pytest.mark.asyncio
async def test_hard_cancelled_runner_cancels_and_reaps_stream_task(monkeypatch):
    """Cancelling the consumer of ``run()`` mid-stream interrupts the scripted
    slow provider stream (the stream task sees CancelledError) instead of
    letting it run on as an orphan."""
    state = {"stream_cancelled": False, "stream_exited": False}

    async def slow_stream_llm(ctx, messages, delta_queue):
        try:
            while True:
                await delta_queue.put(("delta", "chunk "))
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            state["stream_cancelled"] = True
            raise
        finally:
            state["stream_exited"] = True

    monkeypatch.setattr(LLMLoopRunner, "_stream_llm", staticmethod(slow_stream_llm))

    ctx = LoopContext(project_id="project-a")
    events = []

    async def consume():
        async for event in LLMLoopRunner().run(ctx, []):
            events.append(event)

    runner_task = asyncio.create_task(consume())

    # Wait until the stream is actually in flight (first delta observed).
    for _ in range(500):
        if any(e.get("type") == "delta" for e in events):
            break
        await asyncio.sleep(0.01)
    assert any(e.get("type") == "delta" for e in events), "stream never started"

    runner_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner_task

    # Let the reaped stream task settle, then require it to be gone.
    await asyncio.sleep(0.1)
    assert state["stream_cancelled"]
    assert state["stream_exited"]
