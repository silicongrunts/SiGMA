"""
Unit tests for browser tools.

Covers the audit-driven fixes:
- Fix 1: per-tab console filtering (event tagging + read-time filter)
- Fix 4: browser_vision rejects empty question at handler entry
- Fix 6: _bring_to_front has a soft timeout, never blocks the caller
- Fix 8: _disconnect compatibility alias is gone
- Fix 9: _take_snapshot early-returns when the page is already closed
- Fix 10: clear=true combined with action='execute' clears the buffer
- Fix 2 (regression): all browser tools stay read-only and reachable from
  every subagent toolset.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.tools.browser_manager import BrowserManager
from app.agents.tools.browser_tools import (
    _browser_console,
    _browser_vision,
    _take_snapshot,
)


# ── Fix 1 — per-tab console event tagging + filtering ───────────────

class _FakePage:
    """Minimal stand-in for playwright.Page used by listener tests."""

    def __init__(self):
        self._listeners: dict[str, callable] = {}
        self._closed = False

    def on(self, event, callback):
        self._listeners[event] = callback

    def fire(self, event, payload):
        cb = self._listeners.get(event)
        if cb is not None:
            cb(payload)

    def is_closed(self):
        return self._closed


class _FakeMsg:
    def __init__(self, text: str, msg_type: str = "log"):
        self.text = text
        self.type = msg_type


@pytest.mark.asyncio
async def test_console_buffer_per_tab_filtering():
    """Events fired on different pages get tagged with their tab_id and
    ``get_console_log(tab_id=...)`` filters accordingly."""
    mgr = BrowserManager()
    page0, page1 = _FakePage(), _FakePage()
    mgr._pages = [
        {"id": "tab-0", "page": page0},
        {"id": "tab-1", "page": page1},
    ]

    mgr._attach_listeners(page0)
    mgr._attach_listeners(page1)

    page0.fire("console", _FakeMsg("hello from tab0"))
    page1.fire("console", _FakeMsg("hello from tab1", "warn"))
    page0.fire("pageerror", Exception("err from tab0"))

    all_entries = mgr.get_console_log()
    assert len(all_entries) == 3
    assert {e["tab_id"] for e in all_entries} == {"tab-0", "tab-1"}

    tab0 = mgr.get_console_log(tab_id="tab-0")
    assert len(tab0) == 2
    assert all(e["tab_id"] == "tab-0" for e in tab0)

    tab1 = mgr.get_console_log(tab_id="tab-1")
    assert len(tab1) == 1
    assert tab1[0]["tab_id"] == "tab-1"


@pytest.mark.asyncio
async def test_console_read_unknown_tab_returns_error():
    """read with an unknown tab_id returns a not-found error listing
    available tabs (same shape as execute mode)."""
    mgr = BrowserManager()
    mgr._pages = [{"id": "tab-0", "page": _FakePage()}]

    with patch("app.agents.tools.browser_tools.get_browser_manager",
               return_value=mgr):
        result = await _browser_console(action="read", tab_id="tab-99")

    assert "tab_id 'tab-99' not found" in result
    assert "tab-0" in result  # available list surfaced


@pytest.mark.asyncio
async def test_console_read_with_valid_tab_id_filters():
    """read with a real tab_id only returns entries from that tab."""
    mgr = BrowserManager()
    mgr._console_buffer.append({"type": "log", "text": "a", "tab_id": "tab-0"})
    mgr._console_buffer.append({"type": "log", "text": "b", "tab_id": "tab-1"})
    mgr._pages = [
        {"id": "tab-0", "page": _FakePage()},
        {"id": "tab-1", "page": _FakePage()},
    ]

    with patch("app.agents.tools.browser_tools.get_browser_manager",
               return_value=mgr):
        result = await _browser_console(action="read", tab_id="tab-0")

    assert "[log] a" in result
    assert "[log] b" not in result


# ── Fix 4 — browser_vision empty-question validation ────────────────

@pytest.mark.asyncio
async def test_browser_vision_rejects_empty_question():
    """Blank question short-circuits at handler entry — no manager touch."""
    result = await _browser_vision(question="   ")
    assert result == "Error: question is required."


@pytest.mark.asyncio
async def test_browser_vision_rejects_missing_question():
    result = await _browser_vision(question="")
    assert result == "Error: question is required."


# ── Fix 6 — _bring_to_front soft timeout ────────────────────────────

@pytest.mark.asyncio
async def test_bring_to_front_timeout_is_soft():
    """A hanging CDP call must not propagate; detach is still called."""
    page = MagicMock()
    cdp = AsyncMock()
    cdp.send = AsyncMock(side_effect=asyncio.TimeoutError())
    cdp.detach = AsyncMock()
    page.context.new_cdp_session = AsyncMock(return_value=cdp)

    # Should not raise
    await BrowserManager._bring_to_front(page)

    cdp.send.assert_awaited_once_with("Page.bringToFront")
    cdp.detach.assert_awaited_once()


# ── Fix 8 — _disconnect alias removed ───────────────────────────────

def test_disconnect_alias_removed():
    assert not hasattr(BrowserManager, "_disconnect"), (
        "_disconnect compatibility alias should be removed"
    )


# ── Fix 9 — _take_snapshot guards against closed page ───────────────

@pytest.mark.asyncio
async def test_take_snapshot_closed_page_returns_marker():
    """A closed page short-circuits before the 3s settle / DOM build."""
    page = MagicMock()
    page.is_closed.return_value = True

    result = await _take_snapshot(page, mode="dom")

    assert result == "(tab closed during operation)"
    page.is_closed.assert_called_once()


# ── Fix 10 — clear=true + action='execute' clears buffer ────────────

@pytest.mark.asyncio
async def test_browser_console_execute_clears_buffer():
    """execute + clear=True clears the buffer before running JS."""
    mgr = BrowserManager()
    mgr._pages = [{"id": "tab-0", "page": _FakePage()}]
    mgr.clear_console_log = MagicMock()
    # _with_timeout is patched, so mgr.get_page() is never awaited — make it
    # a sync MagicMock to avoid creating an un-awaited coroutine.
    mgr.get_page = MagicMock(return_value=None)

    fake_page = MagicMock()
    fake_page.evaluate = AsyncMock(return_value={"ok": True})

    with patch("app.agents.tools.browser_tools.get_browser_manager",
               return_value=mgr), \
         patch("app.agents.tools.browser_tools._with_timeout",
               new=AsyncMock(return_value=fake_page)):
        result = await _browser_console(
            action="execute", js_code="return {ok: true}", clear=True,
        )

    mgr.clear_console_log.assert_called_once()
    fake_page.evaluate.assert_awaited_once_with("return {ok: true}")
    assert '"ok"' in result


@pytest.mark.asyncio
async def test_browser_console_execute_without_clear_does_not_clear():
    """execute without clear leaves the buffer alone."""
    mgr = BrowserManager()
    mgr._pages = [{"id": "tab-0", "page": _FakePage()}]
    mgr.clear_console_log = MagicMock()
    mgr.get_page = MagicMock(return_value=None)

    fake_page = MagicMock()
    fake_page.evaluate = AsyncMock(return_value=1)

    with patch("app.agents.tools.browser_tools.get_browser_manager",
               return_value=mgr), \
         patch("app.agents.tools.browser_tools._with_timeout",
               new=AsyncMock(return_value=fake_page)):
        await _browser_console(action="execute", js_code="1", clear=False)

    mgr.clear_console_log.assert_not_called()


# ── Fix 2 — all browser tools stay read-only + in ANNOTATION_TOOLS ──

def test_all_browser_tools_are_read_only():
    from app.agents.tools.registry import tool_registry

    browser_tools = [
        t for t in tool_registry.list_all() if t.name.startswith("browser_")
    ]
    assert len(browser_tools) == 10, (
        f"Expected 10 browser tools, got {len(browser_tools)}"
    )
    not_read_only = [t.name for t in browser_tools if not t.is_read_only]
    assert not not_read_only, (
        f"These browser tools must be is_read_only=True: {not_read_only}"
    )


def test_all_browser_tools_in_annotation_tools():
    from app.agents.toolsets import ANNOTATION_TOOLS
    from app.agents.tools.registry import tool_registry

    browser_tool_names = {
        t.name for t in tool_registry.list_all()
        if t.name.startswith("browser_")
    }
    missing = browser_tool_names - set(ANNOTATION_TOOLS)
    assert not missing, (
        f"Browser tools missing from ANNOTATION_TOOLS: {sorted(missing)}"
    )
