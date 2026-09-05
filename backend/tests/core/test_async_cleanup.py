import asyncio

import pytest

from app.core.async_cleanup import finish_cleanup


@pytest.mark.asyncio
async def test_cleanup_finishes_before_repeated_cancellation_propagates():
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def cleanup():
        started.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(finish_cleanup(cleanup()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cleanup_failure_is_not_hidden():
    async def cleanup():
        raise RuntimeError("cleanup failed")

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await finish_cleanup(cleanup())
