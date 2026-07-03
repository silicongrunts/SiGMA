"""
Tab Reaper — closes idle Chrome tabs via raw CDP HTTP.

Runs as a daemon thread, completely independent of Playwright and
BrowserManager.  Periodically scans Chrome's CDP ``/json/list`` endpoint
and closes tabs whose (url, title) have not changed for longer than
``BROWSER_TAB_IDLE_TIMEOUT`` seconds.

Protected tabs (never closed):
    - about:blank
    - chrome://newtab/

Chrome daemon (``browser_service._chrome_daemon``) will relaunch Chrome
if all tabs are closed and the process exits.
"""

import threading
import time

import httpx

from app.core.config import settings
from app.services.browser_service import CHROME_CDP_PORT
from app.core.logging import get_logger

logger = get_logger(__name__)

_SCAN_INTERVAL = 60  # seconds between scans (hardcoded, not user-facing)


class TabReaper:
    """Daemon thread that closes idle Chrome tabs."""

    def __init__(self, cdp_port: int, idle_timeout: int):
        self._cdp_port = cdp_port
        self._idle_timeout = idle_timeout
        self._base_url = f"http://127.0.0.1:{cdp_port}"
        # targetId -> {"key": (url, title), "first_seen": float}
        self._state: dict[str, dict] = {}
        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="tab-reaper"
        )
        self._thread.start()
        logger.info(
            "TabReaper started (idle_timeout=%ds, scan_interval=%ds)",
            self._idle_timeout, _SCAN_INTERVAL,
        )

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("TabReaper stopped")

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while self._running:
            time.sleep(_SCAN_INTERVAL)
            if not self._running:
                break
            try:
                self._scan()
            except Exception as exc:
                logger.debug("TabReaper scan error: %s", exc, exc_info=True)

    def _scan(self) -> None:
        """One scan cycle: list targets, detect idle, close."""
        try:
            resp = httpx.get(f"{self._base_url}/json/list", timeout=5)
            resp.raise_for_status()
            targets = resp.json()
        except Exception:
            logger.debug("TabReaper could not reach Chrome", exc_info=True)
            return  # Chrome not reachable — skip

        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            self._state.clear()
            return

        now = time.time()
        new_state: dict[str, dict] = {}

        # Build current state — preserve first_seen when (url, title) unchanged
        for t in pages:
            tid = t["id"]
            key = (t.get("url", ""), t.get("title", ""))

            old = self._state.get(tid)
            if old and old["key"] == key:
                new_state[tid] = {"key": key, "first_seen": old["first_seen"]}
            else:
                new_state[tid] = {"key": key, "first_seen": now}

        self._state = new_state

        # Close idle tabs
        closed = 0
        for t in pages:
            tid = t["id"]
            info = new_state.get(tid)
            if not info:
                continue

            url = info["key"][0]

            # Protected URLs — never close
            if url in ("about:blank", "chrome://newtab/"):
                continue

            idle = now - info["first_seen"]
            if idle < self._idle_timeout:
                continue

            try:
                r = httpx.get(f"{self._base_url}/json/close/{tid}", timeout=5)
                if r.status_code == 200:
                    closed += 1
                    logger.info(
                        "TabReaper closed idle tab %s (url=%s, idle=%.0fs)",
                        tid, url[:100], idle,
                    )
                    self._state.pop(tid, None)
            except Exception as exc:
                logger.debug("TabReaper close %s failed: %s", tid, exc, exc_info=True)

        if closed:
            logger.info("TabReaper closed %d idle tab(s)", closed)


# ==================================================================
# Singleton
# ==================================================================

_tab_reaper: TabReaper | None = None


def get_tab_reaper() -> TabReaper:
    global _tab_reaper
    if _tab_reaper is None:
        _tab_reaper = TabReaper(
            cdp_port=CHROME_CDP_PORT,
            idle_timeout=settings.BROWSER_TAB_IDLE_TIMEOUT,
        )
    return _tab_reaper
