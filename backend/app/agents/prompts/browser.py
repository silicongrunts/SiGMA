"""
Browser tool prompts — detailed instructions for the LLM.

Each constant is used by the corresponding tool's ToolDefinition.prompt field.
"""

PROMPT_BROWSER_NAVIGATE = """Navigate to a URL or search the web in the shared browser.

If the input looks like a URL (starts with http://, https://, or is a valid domain),
navigates directly. Otherwise, treats the input as a search query and uses the
configured search engine.

Leave tab_id empty to OPEN A NEW TAB. Pass a tab_id to navigate an existing tab.

The mode parameter controls the return format:
- mode='dom' (default): Enhanced DOM snapshot with element refs for interaction.
- mode='markdown': Readable Markdown for understanding page content.

Three ref types appear in dom mode:
- [ref=eN] — interactive DOM element (button, link, input, etc.). Use with browser_click.
- [ref=fN] — folded content (consecutive repeated elements collapsed). Click to expand.
- [ref=tN] — truncated content (section omitted due to length). Click to expand.

Usage notes:
- Use browser_pages first to see what tabs are already open
- To open a new tab (most common), omit tab_id
- To reuse an existing tab, pass its tab_id
"""

PROMPT_BROWSER_SNAPSHOT = """Get a snapshot of a page's content.

Two modes controlled by the mode parameter:

- mode='dom' (default): Enhanced DOM snapshot with [ref=eN] markers on interactive
  elements, plus [ref=fN] for folded items and [ref=tN] for truncated sections.
  Use this when you need element refs for browser_click, browser_input, etc.
- mode='markdown': Page as readable Markdown. Best for reading articles,
  search results, data tables, and understanding page content.

Ref types in dom mode:
- [ref=eN] — interactive DOM element. Click with browser_click.
- [ref=fN] — folded content (e.g. "5 similar <li> items"). Click to expand.
- [ref=tN] — truncated content with preview of beginning/end. Click to expand.

In markdown mode, when the page exceeds the output limit, the tail is paginated
into [ref=mN] chunks. Use browser_click to expand each chunk.

Usage notes:
- Call browser_snapshot(mode='dom') before interacting with a page
- Element refs are per-tab — a snapshot on tab-0 gives refs for tab-0 only
- Use tab_id to snapshot a specific tab; omit for the current active tab
"""

PROMPT_BROWSER_CLICK = """Click an element or expand content by its reference ID.

Four ref types:
- [ref=eN]: Click a DOM element (button, link, etc.). Performs the click action
  and returns a page snapshot. The mode parameter controls the return format.
- [ref=fN]: Expand folded content. Returns the full list of folded items.
  mode parameter is ignored — always returns dom format.
- [ref=tN]: Expand truncated content. Returns the full truncated section.
  mode parameter is ignored — always returns dom format.
- [ref=mN]: Expand a paginated markdown chunk. Returns the chunk content.
  mode parameter is ignored — always returns the raw markdown text.

Usage notes:
- Use tab_id to click on a specific tab; omit for the active tab
- Element refs (eN) must come from a recent snapshot of the SAME tab
- Virtual refs (fN, tN) also come from the most recent snapshot
- If a ref is stale, call browser_snapshot first to refresh
"""

PROMPT_BROWSER_INPUT = """Input text into a form field by its reference ID.

Clears any existing content before typing. Optionally presses Enter after input.

When submit=true, returns a page snapshot (mode controls the format).
When submit=false, only confirms input success (no snapshot returned).

Usage notes:
- Use tab_id to target a specific tab; omit for the active tab
- The element_ref must point to an input, textarea, or contenteditable element
- Use submit=true to press Enter after input (useful for search boxes and forms)
"""

PROMPT_BROWSER_SCROLL = """Scroll a page and get a snapshot of the new view.

Directions: up, down, top, bottom. Returns a page snapshot showing the
newly visible content. The mode parameter controls the return format.

Usage notes:
- Use tab_id to scroll a specific tab; omit for the active tab
- 'up' and 'down' scroll by a configurable amount (default 500px)
- 'top' and 'bottom' scroll to page edges
"""

PROMPT_BROWSER_CONSOLE = """Get browser console output or execute JavaScript.

Usage notes:
- With action='read': returns recent console.log/warn/error from the ring buffer.
  Pass tab_id to filter to a specific tab; omit for all tabs combined.
- With action='execute': runs JavaScript in the page context and returns the result
- Use clear=true to clear the buffer before reading (helps isolate events)
"""

PROMPT_BROWSER_VISION = """Take a screenshot and analyze it with AI vision.

Captures the page and sends it to the vision model for analysis. Use when you
need to understand visual layout, colors, charts, or elements the text
snapshot doesn't capture well.

Usage notes:
- Use tab_id to screenshot a specific tab; omit for the active tab
- The prompt parameter guides what the vision model focuses on when a separate
  vision model is needed
- Optionally crop to a specific element ref
"""

PROMPT_BROWSER_BACK = """Navigate back in browser history. Returns a page snapshot.

The mode parameter controls the return format:
- mode='dom' (default): Enhanced DOM with element refs.
- mode='markdown': Readable Markdown.

Usage notes:
- Use tab_id to go back on a specific tab; omit for the active tab
"""

PROMPT_BROWSER_CDP = """Send a raw Chrome DevTools Protocol (CDP) command.

Power-user escape hatch for operations not covered by other browser tools.
Use only when other tools cannot achieve your goal.

Usage notes:
- Use tab_id to target a specific tab; omit for the active tab
- method is a CDP command name (e.g. 'Runtime.evaluate', 'Network.enable')
- params is an optional object with the command parameters
"""

PROMPT_BROWSER_PAGES = """List all open browser tabs.

Returns a table showing each tab's ID, URL, title, and which is active.
The active tab (marked with *) is the one VNC displays and the one
most operations default to when no tab_id is given.

Usage notes:
- Call this before cross-tab workflows to see what tabs are available
- Tab IDs (tab-0, tab-1, ...) are used by other browser tools' tab_id parameter
"""
