import asyncio
from collections.abc import Awaitable
from typing import TypeVar


Result = TypeVar("Result")


async def finish_cleanup(cleanup: Awaitable[Result]) -> Result:
    """Drain bounded resource cleanup even if its owner is cancelled again."""
    task = asyncio.ensure_future(cleanup)
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result
