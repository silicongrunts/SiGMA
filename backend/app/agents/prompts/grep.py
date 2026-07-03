"""
Prompt for the grep tool.
"""

PROMPT = """A powerful search tool built on ripgrep.

Usage:
- ALWAYS use grep for search tasks. NEVER invoke `grep` or `rg` as a Bash command — this tool has been optimized for the correct access.
- Supports full regex syntax (e.g. "log.*Error", "function\\s+\\w+").
- path accepts absolute host paths or project-relative paths. Filter files with `glob` or `type`.
- Output modes (parameter `output_mode`):
    * "files_with_matches" (default) — returns matching file paths only.
    * "content" — returns matching lines (with file path and line number prefix).
    * "count" — returns "filename:count" per file plus a total.
- Context lines: use `-A`, `-B`, `-C`, or `context` (alias for `-C`; wins if both supplied).
- `multiline: true` enables cross-line matching (rg `-U --multiline-dotall`).
- Case-insensitive search: `-i: true`. Line numbers in content mode: `-n: true` (default).
- `head_limit` caps output (default 250); 0 or negative means unlimited. `offset` skips result entries and must be >=0.
- When truncation occurs, a "[Showing results with pagination = limit: N, offset: M]" suffix is appended so you know to paginate with `offset`.
- Pattern syntax: ripgrep regex. Literal braces need escaping (e.g. `interface\\{\\}` to find `interface{}`).
- If ripgrep is not installed, the tool falls back to GNU grep with a reduced feature set; unsupported parameters are reported in the output rather than silently dropped.

Output: matching lines or file paths on success; "No matches for '{pattern}'" if nothing matched."""
