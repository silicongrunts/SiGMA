"""LLMLoopRunner.execute_tool_cancellable contracts.

Cancellation must beat an in-flight tool within a bounded time, exceptions
must propagate unchanged, an outer cancel (worker shutdown) must still run
the tool's CancelledError cleanup, and a missing cancel event degrades to a
plain await.
"""

import asyncio
import time

import pytest

from app.services.llm_loop_runner import LLMLoopRunner


@pytest.mark.asyncio
async def test_execute_tool_cancellable_cancels_slow_tool():
    cancel_event = asyncio.Event()

    async def set_cancel_later():
        await asyncio.sleep(0.1)
        cancel_event.set()

    setter = asyncio.create_task(set_cancel_later())
    start = time.monotonic()
    result = await LLMLoopRunner.execute_tool_cancellable(
        cancel_event, asyncio.sleep(30))
    elapsed = time.monotonic() - start
    assert result == "Tool cancelled by user."
    assert elapsed < 5.0
    assert setter.done()


@pytest.mark.asyncio
async def test_execute_tool_cancellable_propagates_exceptions():
    async def boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await LLMLoopRunner.execute_tool_cancellable(asyncio.Event(), boom())


@pytest.mark.asyncio
async def test_execute_tool_cancellable_outer_cancel_cleans_up_tool():
    """Cancelling the runner task itself (worker shutdown) must not orphan
    the in-flight tool — its CancelledError handler is what kills the
    tool's subprocesses."""
    cancel_event = asyncio.Event()
    tool_cancelled = asyncio.Event()
    tool_started = asyncio.Event()

    async def slow_tool():
        tool_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise

    task = asyncio.create_task(
        LLMLoopRunner.execute_tool_cancellable(cancel_event, slow_tool()))
    await tool_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tool_cancelled.is_set()


@pytest.mark.asyncio
async def test_execute_tool_cancellable_without_event_awaits_normally():
    result = await LLMLoopRunner.execute_tool_cancellable(None, _return("ok"))
    assert result == "ok"


async def _return(value):
    await asyncio.sleep(0)
    return value
