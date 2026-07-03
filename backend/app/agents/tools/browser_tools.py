"""
Browser automation tools — navigate, snapshot, click, input, scroll, etc.

Every tool connects through BrowserManager (Playwright → Chrome CDP).
Multi-tab support: every tool accepts optional 'tab_id'. Without tab_id,
the current active tab is used. VNC follows via Page.bringToFront.

Tools that return page content accept a 'mode' parameter:
- mode='dom' (default): enhanced DOM with [ref=eN] element markers
- mode='markdown': readable Markdown for content consumption
"""

import asyncio
import base64
import json
import re
from urllib.parse import quote

import html2text

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.tools.browser_manager import get_browser_manager
from app.agents.tools.browser_thread import dispatch as _dispatch
from app.agents.tools.dom_service import get_dom_service
from app.agents.prompts import (
    PROMPT_BROWSER_NAVIGATE,
    PROMPT_BROWSER_SNAPSHOT,
    PROMPT_BROWSER_CLICK,
    PROMPT_BROWSER_INPUT,
    PROMPT_BROWSER_SCROLL,
    PROMPT_BROWSER_CONSOLE,
    PROMPT_BROWSER_VISION,
    PROMPT_BROWSER_BACK,
    PROMPT_BROWSER_CDP,
    PROMPT_BROWSER_PAGES,
)
from app.core.config import settings
from app.core.exceptions import (
    BrowserNotConnectedError,
)
from app.core.logging import get_logger
from app.core.model_config import model_role_accepts_images

logger = get_logger(__name__)

_TIMEOUT = settings.BROWSER_TOOL_TIMEOUT
_LONG_TIMEOUT = 120
_NAV_TIMEOUT_MS = _TIMEOUT * 1000

# Settle time for JS-rendered content (shared by dom and markdown modes)
_SETTLE_SECONDS = 3


# ——————————————————————————————————————————————————————————————————
# Helpers
# ——————————————————————————————————————————————————————————————————


async def _with_timeout(coro, seconds: int, label: str):
    """Run coro with a deadline. Returns descriptive error on timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Timeout (%ds): %s", seconds, label)
        return (
            f"Browser operation timed out after {seconds}s: {label}. "
            "The page may be slow to load or unresponsive. "
            "Try a different URL or check with browser_snapshot."
        )


def _tab_label(tab_id: str) -> str:
    """Short label for timeout messages."""
    return f"tab={tab_id}" if tab_id else "active tab"


# ── html2text converter (singleton) ──
_h2t = html2text.HTML2Text()
_h2t.ignore_links = False
_h2t.ignore_images = True
_h2t.ignore_emphasis = False
_h2t.body_width = 0  # no line wrapping
_h2t.protect_links = True
_h2t.wrap_links = False


def _html_to_markdown(html: str) -> str:
    """Convert page HTML to clean Markdown."""
    return _h2t.handle(html).strip()


# ── Markdown pagination (mN virtual refs, mirrors DOM mode tN refs) ───

_MARKDOWN_CHUNK_SIZE = 20000


def _paginate_markdown(text: str, limit: int) -> tuple[str, dict[str, str]]:
    """Truncate markdown at the end with paginated mN virtual refs."""
    if len(text) <= limit:
        return text, {}

    virtual_refs: dict[str, str] = {}
    result = text[:limit]
    rest = text[limit:]

    if rest.strip():
        _store_md_paginated("m1", rest, virtual_refs)
        result += _build_md_trunc_marker("m1", rest)

    return result, virtual_refs


def _store_md_paginated(
    ref_key: str, content: str, virtual_refs: dict[str, str]
) -> None:
    """Store markdown content as a virtual ref, paginating if needed."""
    if len(content) <= _MARKDOWN_CHUNK_SIZE:
        virtual_refs[ref_key] = content
        return

    chunk = content[:_MARKDOWN_CHUNK_SIZE]
    rest = content[_MARKDOWN_CHUNK_SIZE:]

    sub_key = f"{ref_key}-a"
    _store_md_paginated(sub_key, rest, virtual_refs)

    virtual_refs[ref_key] = chunk + "\n" + _build_md_trunc_marker(sub_key, rest)


def _build_md_trunc_marker(ref_key: str, content: str) -> str:
    """Build a clickable truncation marker for markdown content."""
    preview_beginning = content[:200].strip()
    preview_end = content[-200:].strip()
    return (
        f"\n[ref={ref_key}]\n"
        f"--- truncated markdown ({len(content)} chars, click to expand) ---\n"
        f'  Beginning: "{preview_beginning[:150]}..."\n'
        f'  End: "...{preview_end[-150:]}"\n'
        f"---\n"
        f"[/ref={ref_key}]\n"
    )


def _is_url(text: str) -> bool:
    """Check if text looks like a URL vs a search query."""
    text = text.strip()
    if text.startswith(("http://", "https://", "ftp://", "file://")):
        return True
    if re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9-]*\.)+[a-zA-Z]{2,}(:\d+)?(/.*)?$', text):
        return True
    return False


async def _wait_for_load(page, timeout: int = 15):
    """Wait for page load event only (no extra settle)."""
    try:
        await asyncio.wait_for(
            page.wait_for_load_state("load"),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, TimeoutError):
        # 'load' may never fire on long-poll / SPA pages; the post-load
        # settle (_SETTLE_SECONDS in _take_snapshot) handles content readiness.
        pass


async def _take_snapshot(page, mode: str = "dom") -> str:
    """Unified page snapshot entry: settle → dom or markdown.

    Waits _SETTLE_SECONDS for JS rendering, then captures page content
    in the requested format.

    Markdown mode uses the same CDP visibility pipeline as DOM mode
    (via DomService.build_clean_html), then converts to Markdown.
    """
    if page.is_closed():
        return "(tab closed during operation)"

    await asyncio.sleep(_SETTLE_SECONDS)

    if mode == "markdown":
        dom = get_dom_service()
        cleaned_html = await dom.build_clean_html(page)
        md_text = _html_to_markdown(cleaned_html)
        limit = settings.BROWSER_DOM_MAX_CHARS
        if len(md_text) > limit:
            text, virtual_refs = _paginate_markdown(md_text, limit)
            mgr = get_browser_manager()
            mgr.store_virtual_refs(page, virtual_refs)
            return text
        return md_text
    else:
        dom = get_dom_service()
        text, refs, virtual_refs = await dom.build_snapshot(page)
        mgr = get_browser_manager()
        mgr.store_refs(page, refs)
        mgr.store_virtual_refs(page, virtual_refs)
        return text


# ==================================================================
# Tool 1 — browser_navigate
# ==================================================================


async def _browser_navigate(
    url: str,
    mode: str = "dom",
    tab_id: str = "",
    wait_until: str = "domcontentloaded",
) -> str:
    """Navigate to a URL or search query."""
    mgr = get_browser_manager()
    try:
        if _is_url(url):
            target = url if "://" in url else f"https://{url}"
        else:
            target = f"{settings.BROWSER_SEARCH_ENGINE_URL}{quote(url)}"

        if tab_id:
            page = await _with_timeout(
                mgr.get_page(tab_id), _TIMEOUT, f"get_page(tab={tab_id})"
            )
            if isinstance(page, str):
                return page
            await page.goto(target, wait_until=wait_until, timeout=_NAV_TIMEOUT_MS)
            new_tab = False
        else:
            # Ensure connected before checking active tab
            await mgr.ensure_connected()
            # Reuse the active tab if it's on a blank page
            active_entry = mgr._active_entry()
            active_page = active_entry["page"]
            if (
                not active_page.is_closed()
                and active_page.url in ("about:blank", "chrome://newtab/")
            ):
                page = active_page
                tab_id = active_entry["id"]
                await page.goto(target, wait_until=wait_until, timeout=_NAV_TIMEOUT_MS)
                new_tab = False
            else:
                result = await _with_timeout(
                    mgr.create_page(target), _TIMEOUT, "create_page()"
                )
                if isinstance(result, str):
                    return result
                page = mgr._active_entry()["page"]
                tab_id = result["id"]
                new_tab = True

        await _wait_for_load(page)

        snapshot_text = await _take_snapshot(page, mode)

        title = await page.title()
        prefix = f"Opened new tab {tab_id}" if new_tab else f"Navigated tab {tab_id}"
        return (
            f"{prefix} → {page.url}\n"
            f"Title: {title}\n\n"
            f"{snapshot_text}"
        )

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_navigate('%s') failed: %s", url, msg, exc_info=True)
        return f"Navigation to '{url}' failed: {msg}."


# ==================================================================
# Tool 2 — browser_snapshot
# ==================================================================


async def _browser_snapshot(
    mode: str = "dom",
    tab_id: str = "",
) -> str:
    mgr = get_browser_manager()
    try:
        tid = tab_id or None
        page = await _with_timeout(
            mgr.get_page(tid), _TIMEOUT, f"get_page({_tab_label(tab_id)})"
        )
        if isinstance(page, str):
            return page

        snapshot_text = await _take_snapshot(page, mode)
        return snapshot_text

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_snapshot failed: %s", msg, exc_info=True)
        return f"Snapshot error: {msg}"


# ==================================================================
# Tool 3 — browser_click
# ==================================================================


async def _browser_click(
    element_ref: str,
    mode: str = "dom",
    tab_id: str = "",
) -> str:
    mgr = get_browser_manager()
    try:
        # Virtual ref (fold/trunc) → always return dom content
        if mgr.is_virtual_ref(element_ref):
            content = mgr.resolve_virtual_ref(element_ref)
            if content:
                return f"Expanded content for {element_ref}:\n\n{content}"
            return (
                f"Virtual ref '{element_ref}' is stale. "
                "Take a fresh snapshot to get updated refs."
            )

        # Normal ref → click element + return snapshot
        tid = tab_id or None
        page = await _with_timeout(
            mgr.get_page(tid), _TIMEOUT, f"get_page({_tab_label(tab_id)})"
        )
        if isinstance(page, str):
            return page

        try:
            backend_node_id = mgr.resolve_ref(element_ref)
        except ValueError as e:
            return str(e)

        ok = await mgr.click_element(page, backend_node_id)
        if not ok:
            return (
                f"Element '{element_ref}' has no clickable position. "
                "Run browser_snapshot to get current elements."
            )

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            logger.debug("Browser click load-state wait failed", exc_info=True)

        snapshot_text = await _take_snapshot(page, mode)

        return (
            f"Clicked {element_ref} in tab {mgr._active_entry()['id']}. "
            f"URL: {page.url}\n\n"
            f"{snapshot_text}"
        )

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_click('%s') failed: %s", element_ref, msg, exc_info=True)
        return f"Click on '{element_ref}' failed: {msg}"


# ==================================================================
# Tool 4 — browser_input
# ==================================================================


async def _browser_input(
    element_ref: str,
    text: str,
    mode: str = "dom",
    tab_id: str = "",
    clear_first: bool = True,
    submit: bool = False,
) -> str:
    mgr = get_browser_manager()
    try:
        tid = tab_id or None
        page = await _with_timeout(
            mgr.get_page(tid), _TIMEOUT, f"get_page({_tab_label(tab_id)})"
        )
        if isinstance(page, str):
            return page

        try:
            backend_node_id = mgr.resolve_ref(element_ref)
        except ValueError as e:
            return str(e)

        ok = await mgr.input_text(
            page, backend_node_id, text,
            clear_first=clear_first, submit=submit,
        )
        if not ok:
            return f"Input into '{element_ref}' failed. Run browser_snapshot to refresh."

        if submit:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                logger.debug("Browser input load-state wait failed", exc_info=True)

            snapshot_text = await _take_snapshot(page, mode)

            return (
                f"Input '{text}' into {element_ref} and submitted "
                f"in tab {mgr._active_entry()['id']}. URL: {page.url}\n\n"
                f"{snapshot_text}"
            )

        return f"Input '{text}' into {element_ref} in tab {mgr._active_entry()['id']}."

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_input('%s') failed: %s", element_ref, msg, exc_info=True)
        return f"Input into '{element_ref}' failed: {msg}"


# ==================================================================
# Tool 5 — browser_scroll
# ==================================================================


async def _browser_scroll(
    direction: str,
    mode: str = "dom",
    amount: int = 500,
    tab_id: str = "",
) -> str:
    mgr = get_browser_manager()
    try:
        tid = tab_id or None
        page = await _with_timeout(
            mgr.get_page(tid), _TIMEOUT, f"get_page({_tab_label(tab_id)})"
        )
        if isinstance(page, str):
            return page

        js_map = {
            "top": "window.scrollTo(0, 0)",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
            "up": f"window.scrollBy(0, -{amount})",
            "down": f"window.scrollBy(0, {amount})",
        }
        js = js_map.get(direction)
        if not js:
            return f"Invalid direction '{direction}'. Use: up, down, top, bottom."

        await page.evaluate(js)

        snapshot_text = await _take_snapshot(page, mode)

        return (
            f"Scrolled {direction} in tab {mgr._active_entry()['id']}.\n\n"
            f"{snapshot_text}"
        )

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_scroll('%s') failed: %s", direction, msg, exc_info=True)
        return f"Scroll error: {msg}"


# ==================================================================
# Tool 6 — browser_console
# ==================================================================


async def _browser_console(
    action: str = "read",
    tab_id: str = "",
    js_code: str = "",
    clear: bool = False,
) -> str:
    mgr = get_browser_manager()
    try:
        if action == "read":
            if clear:
                mgr.clear_console_log()
                return "Console buffer cleared."
            if tab_id and not mgr.has_tab(tab_id):
                return (
                    f"tab_id '{tab_id}' not found. "
                    f"Available: {', '.join(mgr.available_tab_ids())}."
                )
            entries = mgr.get_console_log(tab_id=tab_id or None)
            if not entries:
                return "(no console output captured yet)"
            lines = [f"[{e.get('type', 'log')}] {e.get('text', '')}" for e in entries]
            return "\n".join(lines)

        elif action == "execute":
            if not js_code:
                return "Error: js_code is required when action='execute'."
            if clear:
                mgr.clear_console_log()
            tid = tab_id or None
            page = await _with_timeout(
                mgr.get_page(tid), _TIMEOUT, f"get_page({_tab_label(tab_id)})"
            )
            if isinstance(page, str):
                return page
            result = await page.evaluate(js_code)
            if result is None:
                return "(executed, no return value)"
            return json.dumps(result, ensure_ascii=False, indent=2)

        else:
            return f"Invalid action '{action}'. Use 'read' or 'execute'."

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_console(%s) failed: %s", action, msg, exc_info=True)
        return f"Console error: {msg}"


# ==================================================================
# Tool 7 — browser_vision
# ==================================================================


async def _browser_vision(
    question: str = "",
    tab_id: str = "",
    element_ref: str = "",
    model_role: str = "supervisor",
) -> str | dict:
    if not question.strip():
        return "Error: question is required."
    mgr = get_browser_manager()
    try:
        tid = tab_id or None
        page = await _with_timeout(
            mgr.get_page(tid), _TIMEOUT, f"get_page({_tab_label(tab_id)})"
        )
        if isinstance(page, str):
            return page

        png_bytes = await _with_timeout(
            mgr.take_screenshot(page, element_ref), _TIMEOUT, "take screenshot"
        )
        if isinstance(png_bytes, str):
            return png_bytes

        image_b64 = base64.b64encode(png_bytes).decode("ascii")

        if model_role_accepts_images(model_role):
            return {
                "type": "image",
                "image_base64": image_b64,
                "media_type": "image/png",
                "text": "Browser screenshot captured for direct visual inspection.",
            }

        vision_model = settings.VISION_MODEL
        if not vision_model:
            return "Error: vision model is not configured."

        from app.services.llm_service import llm_service

        analysis = await llm_service.call_vision(
            prompt=question,
            image_base64=image_b64,
        )
        return analysis or "(vision model returned no analysis)"

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_vision failed: %s", msg, exc_info=True)
        return f"Vision analysis error: {msg}"


# ==================================================================
# Tool 8 — browser_back
# ==================================================================


async def _browser_back(
    mode: str = "dom",
    tab_id: str = "",
) -> str:
    mgr = get_browser_manager()
    try:
        tid = tab_id or None
        page = await _with_timeout(
            mgr.get_page(tid), _TIMEOUT, f"get_page({_tab_label(tab_id)})"
        )
        if isinstance(page, str):
            return page

        await page.go_back()

        snapshot_text = await _take_snapshot(page, mode)

        return (
            f"Back in tab {mgr._active_entry()['id']} → {page.url}\n"
            f"Title: {await page.title()}\n\n"
            f"{snapshot_text}"
        )

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_back failed: %s", msg, exc_info=True)
        return f"Back navigation error: {msg}"


# ==================================================================
# Tool 9 — browser_cdp
# ==================================================================


async def _browser_cdp(method: str, tab_id: str = "", params: dict = None) -> str:
    mgr = get_browser_manager()
    try:
        tid = tab_id or None
        page = await _with_timeout(
            mgr.get_page(tid), _TIMEOUT, f"get_page({_tab_label(tab_id)})"
        )
        if isinstance(page, str):
            return page

        cdp = await page.context.new_cdp_session(page)
        try:
            result = await _with_timeout(
                cdp.send(method, params or {}), _LONG_TIMEOUT, f"CDP {method}"
            )
            if isinstance(result, str) and "timed out" in result:
                return result
        finally:
            await cdp.detach()

        if result is None:
            return f"CDP {method}: OK (no result)"
        return json.dumps(result, ensure_ascii=False, indent=2)

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except ValueError as e:
        return str(e)
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_cdp('%s') failed: %s", method, msg, exc_info=True)
        return f"CDP error ({method}): {msg}"


# ==================================================================
# Tool 10 — browser_pages
# ==================================================================


async def _browser_pages() -> str:
    """List all open tabs with id, url, title, and active flag."""
    mgr = get_browser_manager()
    try:
        await mgr.ensure_connected()
        tabs = await mgr.list_pages()
        if not tabs:
            return "(no tabs open. Use browser_navigate to open a page.)"

        lines = [f"{'Tab ID':<8} {'URL':<60} {'Title':<40} {'Active':<7}"]
        lines.append("-" * 115)
        for t in tabs:
            marker = "*" if t["active"] else " "
            url = t["url"][:58] if t["url"] else "(no url)"
            title = t["title"][:38] if t["title"] else ""
            lines.append(f"{t['id']:<8} {url:<60} {title:<40} {marker:<7}")
        return "\n".join(lines)

    except BrowserNotConnectedError:
        return "Browser is not connected. Chromium may be stopped."
    except Exception as e:
        msg = str(e) or type(e).__name__
        logger.error("browser_pages failed: %s", msg, exc_info=True)
        return f"Pages error: {msg}"


# ==================================================================
# Tool registrations
# ==================================================================

# ── Shared mode schema fragment ──
_MODE_PROP = {
    "type": "string",
    "enum": ["dom", "markdown"],
    "description": (
        "Return format: 'dom' = enhanced DOM with element refs, "
        "'markdown' = readable Markdown."
    ),
    "default": "dom",
}

# ── browser_navigate ──
tool_registry.register(ToolDefinition(
    name="browser_navigate",
    description=(
        "Navigate to a URL or search the web. If input is not a URL, "
        "performs a search. Opens a new tab if no tab_id given. "
        "Returns a page snapshot in the chosen mode."
    ),
    prompt=PROMPT_BROWSER_NAVIGATE,
    input_schema={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "URL to navigate to, or search query text "
                    "(e.g. 'github.com' or 'machine learning papers')."
                ),
            },
            "mode": _MODE_PROP,
            "tab_id": {
                "type": "string",
                "description": "Target tab ID (e.g. 'tab-0'). Leave empty to open a NEW tab.",
                "default": "",
            },
            "wait_until": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle"],
                "description": "When to consider navigation done.",
                "default": "domcontentloaded",
            },
        },
        "required": ["url"],
    },
    call=lambda url, mode="dom", tab_id="", wait_until="domcontentloaded":
        _dispatch(_browser_navigate(url, mode, tab_id, wait_until)),
    is_read_only=True,
))

# ── browser_snapshot ──
tool_registry.register(ToolDefinition(
    name="browser_snapshot",
    description=(
        "Get a page snapshot. mode='dom': enhanced DOM with [ref=eN] "
        "element markers for clicking/input. mode='markdown': page as readable "
        "Markdown for reading content."
    ),
    prompt=PROMPT_BROWSER_SNAPSHOT,
    input_schema={
        "type": "object",
        "properties": {
            "mode": _MODE_PROP,
            "tab_id": {
                "type": "string",
                "description": "Target tab ID. Default: active tab.",
                "default": "",
            },
        },
        "required": [],
    },
    call=lambda mode="dom", tab_id="": _dispatch(_browser_snapshot(mode, tab_id)),
    is_read_only=True,
))

# ── browser_click ──
tool_registry.register(ToolDefinition(
    name="browser_click",
    description=(
        "Click an element or expand content by its reference ID. "
        "eN = click DOM element, fN/tN = expand virtual content. "
        "mode only applies to real element refs (eN); virtual refs always return dom."
    ),
    prompt=PROMPT_BROWSER_CLICK,
    input_schema={
        "type": "object",
        "properties": {
            "element_ref": {
                "type": "string",
                "description": (
                    "Element reference from snapshot. eN = click DOM element, "
                    "fN = expand folded items, tN = expand truncated content, "
                    "mN = expand paginated markdown chunk."
                ),
            },
            "mode": _MODE_PROP,
            "tab_id": {
                "type": "string",
                "description": "Target tab ID. Default: active tab.",
                "default": "",
            },
        },
        "required": ["element_ref"],
    },
    call=lambda element_ref, mode="dom", tab_id="": _dispatch(_browser_click(element_ref, mode, tab_id)),
    is_read_only=True,
))

# ── browser_input ──
tool_registry.register(ToolDefinition(
    name="browser_input",
    description=(
        "Type text into an input field identified by its element ref. "
        "Returns a snapshot after submit (mode applies). "
        "Without submit, only confirms input success."
    ),
    prompt=PROMPT_BROWSER_INPUT,
    input_schema={
        "type": "object",
        "properties": {
            "element_ref": {
                "type": "string",
                "description": "Element reference for the input/textarea element.",
            },
            "text": {
                "type": "string",
                "description": "Text to type into the element.",
            },
            "mode": _MODE_PROP,
            "tab_id": {
                "type": "string",
                "description": "Target tab ID. Default: active tab.",
                "default": "",
            },
            "clear_first": {
                "type": "boolean",
                "description": "Clear existing text before typing.",
                "default": True,
            },
            "submit": {
                "type": "boolean",
                "description": "Press Enter after typing.",
                "default": False,
            },
        },
        "required": ["element_ref", "text"],
    },
    call=lambda element_ref, text, mode="dom", tab_id="", clear_first=True, submit=False:
        _dispatch(_browser_input(element_ref, text, mode, tab_id, clear_first, submit)),
    is_read_only=True,
))

# ── browser_scroll ──
tool_registry.register(ToolDefinition(
    name="browser_scroll",
    description="Scroll the page and return a snapshot of the new view.",
    prompt=PROMPT_BROWSER_SCROLL,
    input_schema={
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down", "top", "bottom"],
                "description": "Scroll direction.",
            },
            "mode": _MODE_PROP,
            "amount": {
                "type": "integer",
                "description": "Pixels for up/down scroll. Default: 500.",
                "default": 500,
            },
            "tab_id": {
                "type": "string",
                "description": "Target tab ID. Default: active tab.",
                "default": "",
            },
        },
        "required": ["direction"],
    },
    call=lambda direction, mode="dom", amount=500, tab_id="":
        _dispatch(_browser_scroll(direction, mode, amount, tab_id)),
    is_read_only=True,
))

# ── browser_console ──
tool_registry.register(ToolDefinition(
    name="browser_console",
    description="Read browser console logs or execute JavaScript.",
    prompt=PROMPT_BROWSER_CONSOLE,
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "execute"],
                "description": "'read' for console output; 'execute' to run JS code.",
                "default": "read",
            },
            "tab_id": {
                "type": "string",
                "description": "Target tab ID. Default: active tab.",
                "default": "",
            },
            "js_code": {
                "type": "string",
                "description": "JavaScript to run (only when action='execute').",
            },
            "clear": {
                "type": "boolean",
                "description": (
                    "Clear console buffer before this call. "
                    "With action='read': clears and returns ack only. "
                    "With action='execute': clears before running the JS."
                ),
                "default": False,
            },
        },
        "required": ["action"],
    },
    call=lambda action="read", tab_id="", js_code="", clear=False:
        _dispatch(_browser_console(action, tab_id, js_code, clear)),
    is_read_only=True,
))

# ── browser_vision ──
tool_registry.register(ToolDefinition(
    name="browser_vision",
    description="Take a screenshot and analyze it with AI vision.",
    prompt=PROMPT_BROWSER_VISION,
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What to analyze in the screenshot.",
            },
            "tab_id": {
                "type": "string",
                "description": "Target tab ID. Default: active tab.",
                "default": "",
            },
            "element_ref": {
                "type": "string",
                "description": "Optional: crop screenshot to a specific element.",
                "default": "",
            },
        },
        "required": ["question"],
    },
    call=lambda question="", tab_id="", element_ref="", model_role="supervisor":
        _dispatch(_browser_vision(question, tab_id, element_ref, model_role)),
    requires_model_role=True,
    is_read_only=True,
))

# ── browser_back ──
tool_registry.register(ToolDefinition(
    name="browser_back",
    description="Navigate back in browser history. Returns a page snapshot.",
    prompt=PROMPT_BROWSER_BACK,
    input_schema={
        "type": "object",
        "properties": {
            "mode": _MODE_PROP,
            "tab_id": {
                "type": "string",
                "description": "Target tab ID. Default: active tab.",
                "default": "",
            },
        },
        "required": [],
    },
    call=lambda mode="dom", tab_id="": _dispatch(_browser_back(mode, tab_id)),
    is_read_only=True,
))

# ── browser_cdp ──
tool_registry.register(ToolDefinition(
    name="browser_cdp",
    description="Send a raw Chrome DevTools Protocol (CDP) command.",
    prompt=PROMPT_BROWSER_CDP,
    input_schema={
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "description": "CDP method name. E.g. 'Runtime.evaluate'.",
            },
            "tab_id": {
                "type": "string",
                "description": "Target tab ID. Default: active tab.",
                "default": "",
            },
            "params": {
                "type": "object",
                "description": "Method parameters as a JSON object.",
                "default": {},
            },
        },
        "required": ["method"],
    },
    call=lambda method, tab_id="", params=None: _dispatch(_browser_cdp(method, tab_id, params)),
    is_read_only=True,
))

# ── browser_pages ──
tool_registry.register(ToolDefinition(
    name="browser_pages",
    description="List all open browser tabs with their IDs, URLs, titles, and active status.",
    prompt=PROMPT_BROWSER_PAGES,
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    call=lambda: _dispatch(_browser_pages()),
    is_read_only=True,
))
