"""
Browser Manager — Playwright CDP connection to the shared Chrome instance.

Manages the browser connection lifecycle, multi-tab tracking, element
reference system, console event capture, and screenshots.
Lazy-connects on first use with automatic reconnection.

DOM snapshot building has moved to dom_service.py (CDP-based).

Architecture:
    BrowserManager (singleton)
        └─ playwright.async_api.Browser  (connect_over_cdp → Chrome)
              └─ default context
                    ├─ tab-0: Page   }  all VNC-visible (display :99)
                    ├─ tab-1: Page   }
                    └─ ...

CRITICAL: Never call new_context() — it creates incognito contexts
invisible to Xvfb/VNC. Always use the default context.
"""

import asyncio
import collections
import time
from typing import Optional

import httpx

from app.core.config import settings
from app.services.browser_service import CHROME_CDP_PORT
from app.core.exceptions import BrowserNotConnectedError
from app.core.logging import get_logger

logger = get_logger(__name__)


def _normalise_ref(ref: str) -> str:
    """Normalise an element ref string for lookup.

    Accepts: ``e1``, ``@e1``, ``ref=e1``  →  ``e1``
    """
    s = ref.lstrip("@")
    if s.startswith("ref="):
        s = s[4:]
    return s


class BrowserManager:
    """Singleton managing Playwright connection to Chrome via CDP."""

    def __init__(self):
        self._pw = None               # playwright.async_api.Playwright
        self._browser = None          # playwright.async_api.Browser (CDP-connected)
        self._connected: bool = False

        # ── Multi-tab state ──
        # _pages: list of {"id": "tab-N", "page": Page}
        self._pages: list[dict] = []
        self._tab_counter: int = 0      # next tab ID suffix
        self._active_idx: int = 0       # index into _pages

        # ── Per-tab element refs ──
        # {"tab-0": {"e1": backend_node_id, ...}, "tab-1": {...}}
        self._refs: dict[str, dict[str, int]] = {}

        # ── Per-tab virtual refs (fold/trunc content) ──
        # {"tab-0": {"f1": "folded content...", "t1": "truncated content..."}, ...}
        self._virtual_refs: dict[str, dict[str, str]] = {}

        # ── Shared console ring buffer ──
        self._console_buffer: collections.deque = collections.deque(
            maxlen=settings.BROWSER_CONSOLE_BUFFER_SIZE
        )

        # ── Listener tracking per-page ──
        self._listened_page_ids: set[int] = set()

        self._cdp_port: int = CHROME_CDP_PORT

    # ==================================================================
    # Connection lifecycle
    # ==================================================================

    @property
    def cdp_url(self) -> str:
        """Explicit IPv4 — avoids ::1 ambiguity."""
        return f"http://127.0.0.1:{self._cdp_port}"

    async def _probe_cdp(self) -> bool:
        """Check whether the Chrome CDP endpoint is reachable."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.cdp_url}/json/version")
                return resp.status_code == 200
        except Exception:
            logger.debug("Chrome CDP probe failed", exc_info=True)
            return False

    async def ensure_connected(self) -> None:
        """Ensure a live Playwright connection.

        Runs on the browser thread's persistent event loop — the loop
        never changes, so no loop-ID check is needed.  Reconnects only
        when the browser process disconnects (Playwright detects this).
        """
        if self._connected:
            try:
                if self._browser and self._browser.is_connected():
                    return
            except Exception:
                logger.info("BrowserManager connection is stale, reconnecting", exc_info=True)
            self._connected = False

        last_error = ""
        for attempt in range(1, 4):
            try:
                await self._connect()
                return
            except BrowserNotConnectedError as e:
                last_error = str(e)
                if attempt < 3:
                    backoff = attempt * 2
                    logger.warning("CDP not reachable (attempt %d), retry in %ds",
                                   attempt, backoff)
                    await asyncio.sleep(backoff)
            except Exception as e:
                last_error = str(e) or type(e).__name__
                logger.warning("Connect attempt %d/3 failed: %s", attempt, last_error, exc_info=True)
                if attempt < 3:
                    await asyncio.sleep(2)

        raise BrowserNotConnectedError(
            f"Could not connect to Chrome CDP after 3 attempts. "
            f"Last error: {last_error}"
        )

    async def _connect(self) -> None:
        """Establish Playwright connection to Chrome over CDP.

        Preserves existing tab IDs when reconnecting (matches by URL).
        Creates a new tab only when no pages exist.
        """
        from playwright.async_api import async_playwright

        # 1. Verify CDP endpoint
        if not await self._probe_cdp():
            raise BrowserNotConnectedError(
                f"Chrome CDP endpoint not reachable at {self.cdp_url}."
            )

        # 2. Start Playwright (once per process)
        if self._pw is None:
            self._pw = await async_playwright().start()
            logger.info("Playwright started")

        # 3. Connect to Chrome over CDP
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(
                self.cdp_url, timeout=15_000
            )
        except Exception as e:
            msg = str(e) or type(e).__name__
            logger.error("connect_over_cdp failed: %s", msg)
            raise BrowserNotConnectedError(
                f"Playwright cannot connect to Chrome CDP: {msg}."
            )

        # 4. Enumerate pages in the default context → tabs
        contexts = self._browser.contexts
        if not contexts:
            raise BrowserNotConnectedError("Chrome has no browser contexts.")

        default_ctx = contexts[0]
        existing = list(default_ctx.pages)

        # Snapshot old state for URL-based matching (preserve tab IDs)
        old_active_id: str | None = None
        if self._pages and self._active_idx < len(self._pages):
            old_active_id = self._pages[self._active_idx]["id"]

        old_by_url: dict[str, str] = {}  # url → tab_id
        for entry in self._pages:
            page = entry["page"]
            try:
                url = page.url if not page.is_closed() else ""
            except Exception:
                logger.debug("Failed to read old browser tab URL", exc_info=True)
                url = ""
            if url and url != "about:blank":
                # Only keep first match (avoid duplicates)
                old_by_url.setdefault(url, entry["id"])

        # Clear mutable state — but NOT _tab_counter (keep it increasing)
        self._pages.clear()
        self._refs.clear()
        self._virtual_refs.clear()
        self._listened_page_ids.clear()

        if existing:
            # Phase 1: match new pages to old tab IDs by URL
            matched_old_ids: set[str] = set()
            for p in existing:
                try:
                    url = p.url
                except Exception:
                    logger.debug("Failed to read browser tab URL during reconnect", exc_info=True)
                    url = ""
                matched_id = old_by_url.get(url) if url else None
                if matched_id and matched_id not in matched_old_ids:
                    self._pages.append({"id": matched_id, "page": p})
                    matched_old_ids.add(matched_id)
                    self._attach_listeners(p)
                else:
                    # Phase 2: new page → fresh tab ID
                    tid = f"tab-{self._tab_counter}"
                    self._tab_counter += 1
                    self._pages.append({"id": tid, "page": p})
                    self._attach_listeners(p)

            # Restore active index if old active tab still exists
            if old_active_id:
                idx = self._find_tab(old_active_id)
                self._active_idx = idx if idx is not None else 0
            else:
                self._active_idx = 0
        else:
            # No pages — create one
            tid = f"tab-{self._tab_counter}"
            self._tab_counter += 1
            p = await default_ctx.new_page()
            self._pages.append({"id": tid, "page": p})
            self._active_idx = 0
            self._attach_listeners(p)

        active = self._pages[self._active_idx]
        await self._bring_to_front(active["page"])
        await asyncio.sleep(0.3)
        self._connected = True

        logger.info("BrowserManager connected — %d tab(s), active=%s",
                     len(self._pages), active["id"])

    async def disconnect(self) -> None:
        """Clean up Playwright resources. Called on app shutdown."""
        self._connected = False
        self._pages.clear()
        self._refs.clear()
        self._virtual_refs.clear()
        self._listened_page_ids.clear()
        self._active_idx = 0

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                logger.debug("BrowserManager browser close failed", exc_info=True)
            self._browser = None
            logger.info("BrowserManager browser closed")

        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                logger.debug("BrowserManager playwright stop failed", exc_info=True)
            self._pw = None
            logger.info("BrowserManager playwright stopped")

    # ==================================================================
    # Tab management
    # ==================================================================

    @staticmethod
    async def _bring_to_front(page) -> None:
        """CDP Page.bringToFront — ensures VNC shows this page. Best-effort."""
        try:
            cdp = await page.context.new_cdp_session(page)
            try:
                await asyncio.wait_for(cdp.send("Page.bringToFront"), timeout=3)
            except (asyncio.TimeoutError, TimeoutError):
                logger.debug("bringToFront timed out (best-effort, ignored)")
            finally:
                await cdp.detach()
        except Exception as e:
            logger.debug("bring_to_front failed: %s", e, exc_info=True)

    def _find_tab(self, tab_id: str) -> int | None:
        """Return the index of tab_id in _pages, or None."""
        for i, entry in enumerate(self._pages):
            if entry["id"] == tab_id:
                return i
        return None

    def _remove_closed_tab(self, idx: int) -> None:
        """Remove a closed tab entry and fix up _active_idx."""
        entry = self._pages.pop(idx)
        tab_id = entry["id"]
        self._refs.pop(tab_id, None)
        self._virtual_refs.pop(tab_id, None)
        if self._active_idx >= len(self._pages):
            self._active_idx = max(0, len(self._pages) - 1)

    def _active_entry(self) -> dict:
        """Return the active tab entry."""
        if not self._pages:
            raise BrowserNotConnectedError("No tabs. Navigate to a URL first.")
        return self._pages[self._active_idx]

    async def get_page(self, tab_id: str | None = None):
        """Get a page by tab_id, or the active page if tab_id is None.

        Sets _active_idx to the requested tab and calls bringToFront
        so VNC follows the active tab.
        """
        await self.ensure_connected()

        if tab_id is None:
            # Use active tab
            idx = self._active_idx
            if idx >= len(self._pages):
                idx = self._active_idx = 0
        else:
            idx = self._find_tab(tab_id)
            if idx is None:
                valid = [e["id"] for e in self._pages]
                raise ValueError(
                    f"tab_id '{tab_id}' not found. "
                    f"Available tabs: {', '.join(valid)}. "
                    "Use browser_pages to see all open tabs."
                )
            self._active_idx = idx

        # If the page was closed externally (reaper or VNC user), remove it
        entry = self._pages[idx]
        page = entry["page"]
        if page.is_closed():
            closed_id = entry["id"]
            self._remove_closed_tab(idx)
            raise ValueError(
                f"Tab '{closed_id}' has been closed. "
                "Use browser_pages to see available tabs or "
                "browser_navigate to open a new one."
            )

        await self._bring_to_front(page)
        return page

    async def create_page(self, url: str | None = None) -> dict:
        """Create a new tab in the default context, optionally navigate.

        Returns {"id": "tab-N", "url": ..., "title": ...}
        """
        await self.ensure_connected()
        contexts = self._browser.contexts
        default_ctx = contexts[0]

        page = await default_ctx.new_page()

        tid = f"tab-{self._tab_counter}"
        self._tab_counter += 1
        self._pages.append({"id": tid, "page": page})
        self._active_idx = len(self._pages) - 1

        self._attach_listeners(page)
        await self._bring_to_front(page)

        if url:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=settings.BROWSER_TOOL_TIMEOUT * 1000)

        return {"id": tid, "url": page.url, "title": await page.title()}

    async def list_pages(self) -> list[dict]:
        """Return tab metadata for browser_pages tool.

        Format: [{"id": "tab-0", "url": ..., "title": ..., "active": true}, ...]
        """
        result = []
        for i, entry in enumerate(self._pages):
            page = entry["page"]
            try:
                url = page.url  # sync property
                title = await page.title() if not page.is_closed() else "(closed)"
            except Exception:
                logger.debug("Failed to read browser tab metadata", exc_info=True)
                url = ""
                title = ""
            result.append({
                "id": entry["id"],
                "url": url,
                "title": title,
                "active": i == self._active_idx,
            })
        return result

    def _tab_id_for(self, page) -> str:
        """Find the tab_id for a given Page object."""
        for entry in self._pages:
            if entry["page"] is page:
                return entry["id"]
        raise ValueError(
            f"Page object not found in tab registry "
            f"(have {len(self._pages)} tabs tracked)"
        )

    # ==================================================================
    # Console event capture
    # ==================================================================

    def _attach_listeners(self, page) -> None:
        """Attach console + page-error event listeners (per-page idempotent)."""
        pid = id(page)
        if pid in self._listened_page_ids:
            return
        self._listened_page_ids.add(pid)
        tab_id = self._tab_id_for(page)

        def _on_console(msg):
            self._console_buffer.append({
                "type": msg.type,
                "text": msg.text,
                "time": time.time(),
                "tab_id": tab_id,
            })

        def _on_pageerror(err):
            self._console_buffer.append({
                "type": "error",
                "text": str(err),
                "time": time.time(),
                "tab_id": tab_id,
            })

        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        logger.debug("Console listeners attached to page %d (tab=%s)", pid, tab_id)

    def get_console_log(self, tab_id: str | None = None, max_entries: int = 50) -> list[dict]:
        """Return the most recent console entries, optionally filtered by tab."""
        entries = self._console_buffer
        if tab_id:
            entries = [e for e in entries if e.get("tab_id") == tab_id]
        return list(entries)[-max_entries:]

    def clear_console_log(self) -> None:
        """Clear the console ring buffer."""
        self._console_buffer.clear()

    def has_tab(self, tab_id: str) -> bool:
        """Return True if tab_id currently tracks a page."""
        return self._find_tab(tab_id) is not None

    def available_tab_ids(self) -> list[str]:
        """Return IDs of all currently tracked tabs."""
        return [entry["id"] for entry in self._pages]

    # ==================================================================
    # Element reference system  (per-tab)
    # ==================================================================

    def store_refs(self, page, refs: dict[str, int]) -> None:
        """Store element refs (ref → backend_node_id) generated by DomService."""
        tid = self._tab_id_for(page)
        self._refs[tid] = refs

    def store_virtual_refs(self, page, virtual_refs: dict[str, str]) -> None:
        """Store virtual refs (fold/trunc ref → content) generated by DomService."""
        tid = self._tab_id_for(page)
        self._virtual_refs[tid] = virtual_refs

    def resolve_virtual_ref(self, ref: str) -> str | None:
        """Resolve a virtual ref (fN/tN) → stored content, or None."""
        active_id = self._active_entry()["id"]
        clean = _normalise_ref(ref)
        return self._virtual_refs.get(active_id, {}).get(clean)

    def is_virtual_ref(self, ref: str) -> bool:
        """Check if a ref is a virtual ref (fold/trunc, not a real DOM element).

        Matches fN, tN, and paginated sub-refs like tN-a, tN-a-a, etc.
        """
        clean = _normalise_ref(ref)
        if not clean or clean[0] not in ("f", "t", "m"):
            return False
        # Split into base + sub-ref chain: "t1-a-a" → ["1", "a", "a"]
        parts = clean[1:].split("-")
        if not parts or not parts[0].isdigit():
            return False
        return all(p == "a" for p in parts[1:])

    def _check_ref(self, ref: str) -> int:
        """Resolve a ref like 'e1' or '@e1' → backend_node_id for CDP interaction."""
        active_id = self._active_entry()["id"]
        clean = _normalise_ref(ref)
        ref_map = self._refs.get(active_id, {})
        bid = ref_map.get(clean)
        if bid is None:
            raise ValueError(
                f"Element ref '{ref}' is stale or unknown "
                f"in tab {active_id} (have {len(ref_map)} refs cached). "
                "Call browser_snapshot to get fresh element references."
            )
        return bid

    resolve_ref = _check_ref  # alias

    # ==================================================================
    # CDP element interaction  (click / type / clear via backendNodeId)
    # ==================================================================

    async def click_element(self, page, backend_node_id: int) -> bool:
        """Click an element via CDP using its backendNodeId.

        Flow: scroll into view → get box model → dispatch mouse events.
        Returns True on success.
        """
        cdp = await page.context.new_cdp_session(page)
        try:
            # 1. Scroll element into view
            await cdp.send("DOM.scrollIntoViewIfNeeded", {
                "backendNodeId": backend_node_id,
            })

            # 2. Get coordinates from box model
            box = await cdp.send("DOM.getBoxModel", {
                "backendNodeId": backend_node_id,
            })
            model = box.get("model", {})

            # Prefer content quad, fall back through padding/border/margin
            content: list[float] = []
            for quad_key in ("content", "padding", "border", "margin"):
                content = model.get(quad_key, [])
                if content and len(content) >= 8:
                    break

            if not content or len(content) < 8:
                logger.debug("No box model for backendNodeId=%d", backend_node_id)
                return False

            # Center of the quadrilateral
            x = (content[0] + content[2] + content[4] + content[6]) / 4
            y = (content[1] + content[3] + content[5] + content[7]) / 4

            # 3. Dispatch mouse press + release
            for event_type in ("mousePressed", "mouseReleased"):
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                    "modifiers": 0,
                    "pointerType": "mouse",
                })

            return True

        except Exception as e:
            logger.error("click_element(bid=%d) failed: %s", backend_node_id, e, exc_info=True)
            return False
        finally:
            await cdp.detach()

    async def input_text(
        self,
        page,
        backend_node_id: int,
        text: str,
        clear_first: bool = True,
        submit: bool = False,
    ) -> bool:
        """Type text into an element via CDP using its backendNodeId.

        Flow: focus → clear (optional) → keyDown/char/keyUp per character
        → Enter (if submit). Returns True on success.
        """
        cdp = await page.context.new_cdp_session(page)
        try:
            # 1. Focus
            await cdp.send("DOM.focus", {"backendNodeId": backend_node_id})

            # 2. Clear if requested
            if clear_first:
                await self._clear_element_via_cdp(cdp, backend_node_id)

            # 3. Type each character
            for char in text:
                if char == "\n":
                    key = "Enter"
                    vk = 13
                    code = "Enter"
                    native_char = "\r"
                else:
                    key = char
                    vk = 0
                    code = ""
                    native_char = char

                for event_type in ("keyDown", "char", "keyUp"):
                    params: dict = {
                        "type": event_type,
                        "key": key,
                        "code": code,
                        "windowsVirtualKeyCode": vk,
                    }
                    if event_type == "char":
                        params["text"] = native_char
                    await cdp.send("Input.dispatchKeyEvent", params)

            # 4. Submit via Enter
            if submit:
                for event_type in ("keyDown", "char", "keyUp"):
                    params = {
                        "type": event_type,
                        "key": "Enter",
                        "code": "Enter",
                        "windowsVirtualKeyCode": 13,
                    }
                    if event_type == "char":
                        params["text"] = "\r"
                    await cdp.send("Input.dispatchKeyEvent", params)

            return True

        except Exception as e:
            logger.error("input_text(bid=%d) failed: %s", backend_node_id, e, exc_info=True)
            return False
        finally:
            await cdp.detach()

    async def _clear_element_via_cdp(self, cdp, backend_node_id: int) -> None:
        """Clear an input/textarea/contenteditable by setting value to ''."""
        try:
            node_info = await cdp.send("DOM.resolveNode", {
                "backendNodeId": backend_node_id,
            })
            object_id = (node_info.get("object", {}) or {}).get("objectId")
            if not object_id:
                return

            await cdp.send("Runtime.callFunctionOn", {
                "functionDeclaration": (
                    "function() {"
                    "  const el = this;"
                    "  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {"
                    "    el.value = '';"
                    "  } else if (el.getAttribute('contenteditable')) {"
                    "    el.textContent = '';"
                    "  }"
                    "  el.dispatchEvent(new Event('input', {bubbles: true}));"
                    "  el.dispatchEvent(new Event('change', {bubbles: true}));"
                    "}"
                ),
                "objectId": object_id,
            })
        except Exception as e:
            logger.debug("Clear element via CDP failed (non-critical): %s", e, exc_info=True)

    def validate_ref(self, ref: str) -> bool:
        """Check whether a ref exists in the active tab's mapping."""
        active_id = self._active_entry()["id"]
        return ref.lstrip("@") in self._refs.get(active_id, {})

    # ==================================================================
    # Screenshot
    # ==================================================================

    async def take_screenshot(self, page, element_ref: str = "") -> bytes:
        """Take a PNG screenshot (whole page or single element via backendNodeId)."""
        if element_ref:
            backend_node_id = self._check_ref(element_ref)
            cdp = await page.context.new_cdp_session(page)
            try:
                node_info = await cdp.send("DOM.resolveNode", {
                    "backendNodeId": backend_node_id,
                })
                object_id = (node_info.get("object", {}) or {}).get("objectId")
                if not object_id:
                    raise ValueError(f"Cannot resolve element ref '{element_ref}'")

                result = await cdp.send("Runtime.callFunctionOn", {
                    "functionDeclaration": (
                        "function() {"
                        "  const r = this.getBoundingClientRect();"
                        "  return {x: r.x, y: r.y, w: r.width, h: r.height};"
                        "}"
                    ),
                    "objectId": object_id,
                    "returnByValue": True,
                })
                rect = (result.get("result", {}) or {}).get("value", {})
                if rect:
                    return await page.screenshot(type="png", clip={
                        "x": rect["x"], "y": rect["y"],
                        "width": rect["w"], "height": rect["h"],
                        "scale": 1,
                    }, full_page=False)
            except Exception as e:
                logger.warning("Element screenshot via CDP failed: %s", e)
                raise
            finally:
                await cdp.detach()

        return await page.screenshot(type="png", full_page=False)



# ==================================================================
# Singleton
# ==================================================================

_browser_manager: Optional[BrowserManager] = None


def get_browser_manager() -> BrowserManager:
    global _browser_manager
    if _browser_manager is None:
        _browser_manager = BrowserManager()
    return _browser_manager
