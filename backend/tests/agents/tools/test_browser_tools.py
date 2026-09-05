"""
Unit tests for browser tools.

Grouped by behavior:
- Console log filtering: per-tab event tagging and read-time filtering,
  plus buffer clearing semantics for execute mode.
- Vision validation: browser_vision rejects an empty question at handler
  entry, before any manager access.
- Snapshot protection: _take_snapshot early-returns when the page is
  already closed.
- Input validation: browser_input only types into typeable elements, and
  _bring_to_front never blocks the caller on a hanging CDP call.
- Registration: all browser tools stay read-only and reachable from every
  subagent toolset.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.tools.browser_manager import BrowserManager
from app.agents.tools.browser_tools import (
    _browser_console,
    _browser_input,
    _browser_vision,
    _take_snapshot,
)


# ── console log filtering ───────────────────────────────────────────

class _FakePage:
    """Minimal stand-in for playwright.Page used by listener tests."""

    def __init__(self, fail_close=False, url="about:blank"):
        self._listeners: dict[str, callable] = {}
        self._closed = False
        self._fail_close = fail_close
        self.url = url

    def on(self, event, callback):
        self._listeners[event] = callback

    def fire(self, event, payload):
        cb = self._listeners.get(event)
        if cb is not None:
            cb(payload)

    def is_closed(self):
        return self._closed

    async def close(self):
        if self._fail_close:
            raise RuntimeError("close failed")
        self._closed = True


@pytest.mark.asyncio
async def test_dispatch_if_running_does_not_create_an_operation_without_browser_thread():
    from app.agents.tools import browser_thread

    operation = MagicMock()
    with patch.object(browser_thread, "_browser_thread", None):
        assert await browser_thread.dispatch_if_running(operation) is None

    operation.assert_not_called()


@pytest.mark.asyncio
async def test_close_owned_tabs_only_removes_matching_sessions():
    mgr = BrowserManager()
    owned = _FakePage()
    unrelated = _FakePage()
    mgr._pages = [
        {"id": "tab-0", "page": owned, "project_id": "p1", "session_id": "s1"},
        {"id": "tab-1", "page": unrelated, "project_id": "p1", "session_id": "s2"},
    ]

    assert await mgr.close_owned_tabs(session_ids=["s1"]) == 1

    assert owned.is_closed()
    assert not unrelated.is_closed()
    assert mgr.available_tab_ids() == ["tab-1"]


@pytest.mark.asyncio
async def test_close_owned_tabs_keeps_failed_tab_tracked_for_retry():
    mgr = BrowserManager()
    page = _FakePage(fail_close=True)
    mgr._pages = [
        {"id": "tab-0", "page": page, "project_id": "p1", "session_id": "s1"},
    ]

    with pytest.raises(RuntimeError, match="close failed"):
        await mgr.close_owned_tabs(session_ids=["s1"])

    assert mgr.available_tab_ids() == ["tab-0"]


@pytest.mark.asyncio
async def test_reconnect_preserves_ownership_for_duplicate_urls(monkeypatch):
    mgr = BrowserManager()
    mgr._pages = [
        {"id": "tab-0", "page": _FakePage(url="https://example.com"),
         "project_id": "p1", "session_id": "s1"},
        {"id": "tab-1", "page": _FakePage(url="https://example.com"),
         "project_id": "p1", "session_id": "s2"},
    ]

    browser = MagicMock()
    browser.contexts = [MagicMock(pages=[
        _FakePage(url="https://example.com"),
        _FakePage(url="https://example.com"),
    ])]
    chromium = MagicMock()
    chromium.connect_over_cdp = AsyncMock(return_value=browser)
    mgr._pw = MagicMock(chromium=chromium)

    monkeypatch.setattr(mgr, "_probe_cdp", AsyncMock(return_value=True))
    monkeypatch.setattr(mgr, "_bring_to_front", AsyncMock())
    monkeypatch.setattr("app.agents.tools.browser_manager.asyncio.sleep", AsyncMock())

    await mgr._connect()

    assert [entry["id"] for entry in mgr._pages] == ["tab-0", "tab-1"]
    assert [entry["session_id"] for entry in mgr._pages] == ["s1", "s2"]


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


# ── vision validation ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_browser_vision_rejects_empty_question():
    """Blank question short-circuits at handler entry — no manager touch."""
    result = await _browser_vision(question="   ")
    assert result == "Error: question is required."


@pytest.mark.asyncio
async def test_browser_vision_rejects_missing_question():
    result = await _browser_vision(question="")
    assert result == "Error: question is required."


# ── snapshot protection ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_take_snapshot_closed_page_returns_marker():
    """A closed page short-circuits before the 3s settle / DOM build."""
    page = MagicMock()
    page.is_closed.return_value = True

    result = await _take_snapshot(page, mode="dom")

    assert result == "(tab closed during operation)"
    page.is_closed.assert_called_once()


# ── input validation & bring-to-front ───────────────────────────────

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


class _FakeCDP:
    """CDP session stub answering DOM.resolveNode / Runtime.callFunctionOn."""

    def __init__(self, predicate_value: bool, object_id: str | None = "obj-1"):
        self._predicate_value = predicate_value
        self._object_id = object_id
        self.detached = False

    async def send(self, method, params=None):
        if method == "DOM.resolveNode":
            return {"object": {"objectId": self._object_id}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": self._predicate_value}}
        raise AssertionError(f"unexpected CDP call: {method}")

    async def detach(self):
        self.detached = True


@pytest.mark.asyncio
@pytest.mark.parametrize("value,expected", [(True, True), (False, False)])
async def test_is_text_input_reports_element_predicate(value, expected):
    """is_text_input returns the JS predicate result and detaches the session."""
    page = MagicMock()
    cdp = _FakeCDP(predicate_value=value)
    page.context.new_cdp_session = AsyncMock(return_value=cdp)

    assert await BrowserManager.is_text_input(BrowserManager(), page, 42) is expected
    assert cdp.detached


@pytest.mark.asyncio
async def test_is_text_input_detached_node_is_not_text_input():
    """A node that cannot be resolved to a JS object is not typeable."""
    page = MagicMock()
    cdp = _FakeCDP(predicate_value=True, object_id=None)
    page.context.new_cdp_session = AsyncMock(return_value=cdp)

    assert await BrowserManager.is_text_input(BrowserManager(), page, 42) is False
    assert cdp.detached


def _input_manager(is_text_input: bool) -> MagicMock:
    """Manager stub for _browser_input: page resolved, ref e1 -> backendNodeId 7."""
    mgr = MagicMock()
    mgr.get_page = MagicMock(return_value=None)  # _with_timeout is patched
    mgr.resolve_ref = MagicMock(return_value=7)
    mgr.is_text_input = AsyncMock(return_value=is_text_input)
    mgr.input_text = AsyncMock(return_value=True)
    mgr._active_entry = MagicMock(return_value={"id": "tab-0"})
    return mgr


@pytest.mark.asyncio
async def test_browser_input_rejects_non_text_element():
    """Typing into a button/link fails loudly instead of reporting success."""
    mgr = _input_manager(is_text_input=False)

    with patch("app.agents.tools.browser_tools.get_browser_manager",
               return_value=mgr), \
         patch("app.agents.tools.browser_tools._with_timeout",
               new=AsyncMock(return_value=MagicMock())):
        result = await _browser_input(element_ref="e1", text="abc")

    assert result == (
        "Element 'e1' is not a text input (input, textarea, or contenteditable). "
        "Use browser_click for buttons and links."
    )
    mgr.input_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_input_passes_text_element_through():
    """A typeable element flows through to input_text unchanged."""
    mgr = _input_manager(is_text_input=True)

    with patch("app.agents.tools.browser_tools.get_browser_manager",
               return_value=mgr), \
         patch("app.agents.tools.browser_tools._with_timeout",
               new=AsyncMock(return_value=MagicMock())):
        result = await _browser_input(element_ref="e1", text="abc")

    mgr.input_text.assert_awaited_once()
    assert "Input 'abc' into e1" in result


# ── registration: read-only + subagent toolset membership ───────────

def test_all_browser_tools_are_read_only():
    from app.agents.tools.registry import tool_registry

    browser_tools = [
        t for t in tool_registry.list_all() if t.name.startswith("browser_")
    ]
    assert browser_tools, "no browser tools registered"
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
