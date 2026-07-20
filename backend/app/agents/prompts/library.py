"""
Library tool prompts — search, browse, and manage the project knowledge base.
"""

PROMPT_LIBRARY_SEARCH = """Search the project library for documents.

Supports two search modes:
- keyword: SQL-based exact substring match across title, description, and content. Use when searching for specific terms, names, or identifiers.
- semantic: Hybrid vector/BM25 search (RAG) with optional reranking. Use when searching by meaning or concept — provide a short natural language query.

Results are returned as XML:
- keyword mode: each <match> shows the field, the line number where the query first appears in that field, and a snippet around it.
- semantic mode: each <match> shows a relevant chunk with its relevance score and starting line number. The same document may appear with multiple chunks, up to the system's per-document match limit.

Pagination (keyword mode only): use the page parameter (50 results per page, 1-indexed, must be >= 1). The header shows the matching document count and the current page window; advance page to see more. Requesting a page beyond the last one returns an out-of-range notice with the real count. Semantic mode ignores page — it always returns the top-k most relevant chunks.

All document IDs in results are complete IDs. Use these IDs in subsequent tool calls."""

PROMPT_LIBRARY_LS = """List contents of one or more library directories.

Returns subdirectories and documents under each specified parent directory.
The root directory is listed when parent_id is omitted.

All document IDs are complete IDs. Use these in subsequent tool calls."""

PROMPT_LIBRARY_NEW = """Add a new document to the project library.

Three content sources supported:
- text: Plain text content provided directly. Source will be "SiGMA generate".
- file: A file path (absolute, or relative to the project root). Files outside the project sandbox are readable for ingestion. Source will be the original path (relative to project root when the file is inside the project, absolute otherwise).
- tab: A browser tab ID (e.g. "tab-0"). The current page content will be extracted as markdown. Source will be the page URL.

After creation, the document is queued for AI field extraction (description and keywords only — title is never overwritten) and RAG indexing. Both run asynchronously.

All document IDs in output are complete IDs. Use these in subsequent tool calls."""

PROMPT_LIBRARY_MKDIR = """Create a new folder in the library."""

PROMPT_LIBRARY_MV = """Move documents or folders to a target folder.

Performs conflict detection: fails if any source has the same name as an existing item in the destination. Also prevents circular moves (moving a folder into itself or its descendants)."""

PROMPT_LIBRARY_UPDATE = """Update a document's title, description, or content.

Content editing uses exact string replacement (same as the edit tool):
- Provide both old_string and new_string to replace a specific text segment.
- The old_string must be unique in the document. If it appears multiple times, provide more context.
- If only title or description needs changing, omit the content parameters.

Folders: only title updates are supported. Description and content edits on folders are rejected."""

PROMPT_LIBRARY_GET = """Read a document's fields from the library.

Similar to the read tool but for library documents. Supports paginated content reading with line numbers.

offset is 0-indexed and must be >=0. limit: omit or pass 0 for 200; negative returns the last abs(limit) lines. Output uses `cat -n`-style line numbers (`N\\t<content>`) with the document's real 1-indexed line; whenever the window stops short of the end, a "Showing lines X-Y of Z (N more not shown)" footer is appended, matching the read tool.

Documents still processing will include a status notice."""

PROMPT_LIBRARY_RM = """Delete one or more documents or folders from the library.

Accepts a single ID or an array of IDs.

Deleting a folder also deletes all its contents recursively (subfolders and documents). Non-existent IDs are skipped with a warning (not an error).

Output: "Successfully deleted N item(s)" with skipped IDs listed if any."""
