"""
Browser tool prompts — detailed instructions for the LLM.

Each constant is used by the corresponding tool's ToolDefinition.prompt field.
"""

PROMPT_BROWSER_NAVIGATE = """Navigate to a URL or run a web search in the shared browser.

Input that looks like a URL (http://, https://, or a bare domain) is navigated to directly; anything else is treated as a search query for the configured search engine.

- Pass tab_id to navigate an existing tab. Without tab_id: the active tab is reused if it is still on a blank page (about:blank or the new-tab page); otherwise a new tab is opened.
- mode controls the returned snapshot: 'dom' (default) = enhanced DOM with element refs; 'markdown' = readable Markdown.
- wait_until is honored only when an existing tab is reused; opening a new tab always waits for domcontentloaded.
- In dom mode, interactive elements carry [ref=eN] for browser_click; browser_snapshot documents the other ref kinds.

Use browser_pages first to see which tabs are already open."""

PROMPT_BROWSER_SNAPSHOT = """Get a snapshot of a page.

- mode='dom' (default): enhanced DOM with [ref=eN] on interactive elements, [ref=fN] on folded runs of repeated elements, and [ref=tN] on truncated sections. Use when you need refs for browser_click/browser_input.
- mode='markdown': the page as readable Markdown. Use to read articles, search results, and data tables.

When the output exceeds the size limit: markdown mode paginates the omitted tail into chunk ref [ref=m1]; dom mode keeps sections by content density and gives each shortened or dropped section its own [ref=tN] marker (markers for fully dropped sections appear at the end, out of document order). Expanding a chunk may chain continuation refs (m1-a, m1-a-a, ...). Expand any chunk with browser_click.

- Element refs are per-tab. A new dom-mode snapshot of that tab replaces all its previous refs; a markdown snapshot that paginates (one ending in a [ref=m1] marker) also replaces that tab's virtual refs. If a ref no longer resolves, take a new snapshot.
- tab_id selects the tab; omit for the active tab.
- Call browser_snapshot(mode='dom') before interacting with a page."""

PROMPT_BROWSER_CLICK = """Click an element or expand content by its ref.

- [ref=eN]: click the DOM element (button, link, ...) and return a page snapshot; mode controls the format. The ref must come from a recent snapshot of the SAME tab; if it is stale, call browser_snapshot to refresh.
- [ref=fN]: return the complete run of items that was folded. Always returns dom; mode is ignored.
- [ref=tN] / [ref=mN] (and their -a continuations): return the stored chunk content — dom format for tN, raw markdown for mN. mode is ignored.
- tab_id applies only to eN clicks. Virtual refs (fN/tN/mN) always resolve against the latest snapshot of the active tab."""

PROMPT_BROWSER_INPUT = """Type text into a form field selected by its element ref.

- clear_first (default true) clears the field before typing; set false to append.
- Each newline in text is sent as an Enter keypress; literal newlines cannot be typed.
- submit=true presses Enter after typing and returns a page snapshot (mode controls the format); submit=false only confirms the input.
- The ref must point to an input, textarea, or contenteditable element.
- tab_id selects the tab; omit for the active tab."""

PROMPT_BROWSER_SCROLL = """Scroll a page and return a snapshot.

- direction: up/down scroll by `amount` pixels (default 500); top/bottom jump to the page edges.
- The snapshot covers the whole document regardless of scroll position. Scrolling mainly triggers lazy-loaded content (waited for before capture) and changes what browser_vision screenshots.
- mode controls the snapshot format ('dom' default, 'markdown').
- tab_id selects the tab; omit for the active tab."""

PROMPT_BROWSER_CONSOLE = """Read the browser console or execute JavaScript.

- action='read': console messages and page errors from all tabs share one 200-entry buffer; the 50 most recent entries are returned. Pass tab_id to filter to one tab — combined output does not label which tab each line came from.
- action='execute': run JavaScript in the page and return the result; tab_id selects the tab (default: the active tab).
- With action='read', clear=true only clears the buffer and returns "Console buffer cleared." (to isolate events: clear, then read in a separate call). With action='execute', clear=true clears the buffer before running the JS."""

PROMPT_BROWSER_VISION = """Take a viewport screenshot and analyze it with the vision model.

Use when the text snapshot misses layout, charts, colors, or other visual state.

- question: what to analyze in the screenshot.
- element_ref optionally crops to an element from the latest snapshot.
- tab_id selects the tab; omit for the active tab."""

PROMPT_BROWSER_BACK = """Go back one step in browser history and return a page snapshot.

The result starts with the new URL and Title, then the snapshot. mode controls the format ('dom' default, 'markdown'). tab_id selects the tab; omit for the active tab."""

PROMPT_BROWSER_CDP = """Send one raw Chrome DevTools Protocol (CDP) command.

Escape hatch for operations no other browser tool covers. Each call opens a fresh CDP session, sends one command, and detaches: no state persists between calls and events are not captured, so stateful command sequences (e.g. Network.enable followed by a later query) do not work.

- method is a CDP command name (e.g. 'Runtime.evaluate'); params is an optional parameter object.
- tab_id selects the tab; omit for the active tab."""

PROMPT_BROWSER_PAGES = """List the open browser tabs.

Returns a table of each tab's ID, URL (truncated to 58 chars), and title (truncated to 38 chars). The active tab is marked with * and is the tab most operations default to when no tab_id is given.

Tab IDs (tab-0, tab-1, ...) are the tab_id values used by the other browser tools."""
