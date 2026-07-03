"""
Prompt for the glob tool.
"""

PROMPT = """Fast file pattern matching tool that works with any codebase size.

- Supports glob patterns like "**/*.js" or "src/**/*.ts", including brace expansion ("*.{ts,tsx}").
- Results are sorted by modification time (newest first), with alphabetical order as a tiebreaker.
- Returns up to 100 results; additional matches are indicated by a "... (N more matches not shown)" suffix.
- When `path` is a relative subdirectory (e.g. "src"), returned paths are relative to the project root (e.g. "src/foo.ts"), so you can pass them directly to read/edit. When `path` is absolute, returned paths are absolute.
- Use this tool when you need to find files by name patterns. For open-ended searches requiring multiple rounds of globbing and grepping, use the agent tool instead.

Output: matching file paths, one per line, on success; "No files matching '{pattern}'" if nothing matched."""
