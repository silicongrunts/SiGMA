"""
Prompt for the grep tool.
"""

PROMPT = """Search file content with ripgrep regex.

Usage:
- Use this tool for content search; do not run grep/rg through bash.
- path accepts absolute host paths or project-relative paths (file or directory); returned paths are project-relative, or absolute when `path` is absolute. Filter files with `glob` or `type`.
- output_mode: "files_with_matches" (default) — matching file paths; "content" — matching lines prefixed with line number, plus the file path when searching more than one file; "count" — "filename:count" per file (bare count when searching a single file).
- Context lines: `-A`, `-B`, `-C`, or `context` (alias for `-C`; wins if both supplied).
- multiline: true enables cross-line matching (rg -U --multiline-dotall).
- -i: true for case-insensitive search. -n: true (default) for line numbers in content mode.
- head_limit caps output (default 250); 0 or negative means unlimited — any tool result is still cut at 50,000 characters and marked "... [truncated]". offset skips result entries and must be >= 0.
- On truncation a "Showing results X-Y of Z (N more not shown)" suffix is appended — paginate with offset.
- Pattern syntax is ripgrep regex; literal braces need escaping (e.g. `interface\\{\\}` to find `interface{}`).
- Searches time out after 15 seconds (reported in the output). If ripgrep is not installed, the tool falls back to GNU grep: output_mode=count, multiline, context flags, and type are unsupported (reported as ignored in the output), and files_with_matches results are not paginated.
- For open-ended searches needing multiple rounds of globbing and grepping, use the agent tool instead.

Output: matching lines or file paths; "No matches for '{pattern}'" if nothing matched."""
