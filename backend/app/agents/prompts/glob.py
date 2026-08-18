"""
Prompt for the glob tool.
"""

PROMPT = """Find files by glob pattern.

Usage:
- Supports glob patterns like "**/*.js" or "src/**/*.ts", including brace expansion ("*.{ts,tsx}").
- Results are sorted by modification time (newest first), alphabetical as tiebreaker.
- Returns up to 100 results; further matches are indicated by a "... (N more matches not shown)" suffix.
- With a relative `path` (e.g. "src"), returned paths are project-relative (e.g. "src/foo.ts") and can be passed directly to read/edit. With an absolute `path`, returned paths are absolute.
- For open-ended searches needing multiple rounds of globbing and grepping, use the agent tool instead.

Output: matching file paths, one per line; "No files matching '{pattern}'" if nothing matched."""
