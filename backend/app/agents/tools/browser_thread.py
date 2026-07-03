"""
Browser Thread — persistent event loop for Playwright operations.

Playwright objects (Browser, Page, CDPSession) are bound to the asyncio
event loop that created them.  Huey worker tasks each use asyncio.run()
which creates a fresh loop, destroying all Playwright state.

This module solves that by owning a dedicated daemon thread with a
never-stopping event loop.  All browser tool functions are dispatched
to this loop via asyncio.run_coroutine_threadsafe().  Tab IDs, element
refs, and virtual refs remain stable across Huey tasks.

Architecture:
    Huey thread (any event loop)
        └── tool call → dispatch(coro)
                              │
                              ▼  run_coroutine_threadsafe
    Browser thread (persistent loop)
        └── BrowserManager (singleton)
              └── Playwright → Chrome CDP
"""

import asyncio
import threading
from typing import Optional

from app.core.exceptions import BrowserNotConnectedError
from app.core.logging import get_logger

logger = get_logger(__name__)


class BrowserThread:
    """Daemon thread owning the persistent asyncio event loop for Playwright."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready: threading.Event = threading.Event()
        self._manager = None  # BrowserManager, created inside the thread
        self._shutting_down: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the browser thread and wait until the event loop is ready."""
        if self._thread is not None and self._thread.is_alive():
            return

        self._shutting_down = False
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="browser-thread",
        )
        self._thread.start()

        if not self._ready.wait(timeout=10):
            raise BrowserNotConnectedError(
                "Browser thread failed to start within 10 seconds."
            )
        logger.info("BrowserThread started with persistent event loop")

    def _run_loop(self) -> None:
        """Thread entry: create event loop, initialise BrowserManager, run forever."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Create BrowserManager singleton inside this thread's loop.
        # The lazy singleton in browser_manager.py will be used when
        # tool functions call get_browser_manager().
        from app.agents.tools.browser_manager import get_browser_manager
        self._manager = get_browser_manager()

        self._ready.set()

        try:
            self._loop.run_forever()
        finally:
            # Graceful cleanup on loop stop
            if self._manager is not None:
                try:
                    self._loop.run_until_complete(self._manager.disconnect())
                except Exception:
                    logger.debug("BrowserThread manager disconnect failed", exc_info=True)
            self._loop.close()
            logger.info("BrowserThread event loop closed")

    def shutdown(self) -> None:
        """Signal the browser thread to stop and wait for cleanup."""
        self._shutting_down = True

        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread is not None:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("Browser thread did not stop within 10s")

        self._loop = None
        self._thread = None
        self._manager = None

    # ── Dispatch ───────────────────────────────────────────────────────

    async def dispatch(self, coro):
        """Schedule *coro* on the browser thread's event loop.

        Called from any event loop (typically a Huey worker's loop).
        Returns when the coroutine completes on the browser thread.
        """
        if self._shutting_down:
            raise BrowserNotConnectedError("Browser thread is shutting down.")
        if self._loop is None or self._loop.is_closed():
            raise BrowserNotConnectedError(
                "Browser thread is not running. "
                "Try again after the service restarts."
            )

        cf_future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        # Bridge concurrent.futures.Future → asyncio.Future on caller's loop
        return await asyncio.wrap_future(cf_future)

    # ── Accessors ──────────────────────────────────────────────────────

    @property
    def manager(self):
        """Direct access to BrowserManager (only safe inside browser thread)."""
        return self._manager


# ── Module-level singleton ────────────────────────────────────────────

_browser_thread: Optional[BrowserThread] = None


def get_browser_thread() -> BrowserThread:
    """Return (and lazily start) the global BrowserThread singleton."""
    global _browser_thread
    if _browser_thread is None:
        _browser_thread = BrowserThread()
        _browser_thread.start()
    return _browser_thread


async def dispatch(coro):
    """Schedule *coro* on the browser thread's event loop.  Awaitable."""
    return await get_browser_thread().dispatch(coro)
