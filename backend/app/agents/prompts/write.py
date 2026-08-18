"""
Prompt for the write tool.
"""

PROMPT = """Write a file to the local filesystem, overwriting any existing file at the path.

Usage:
- For an existing file, you MUST read it with the read tool first. The tool fails if the file was never read or changed on disk since the last read; a compaction resets this state — re-read before writing.
- Prefer edit for modifying existing files (it only sends the diff). Use write to create new files or for complete rewrites.
- NEVER create documentation files (*.md) or README files unless the user explicitly requests them.
- Only use emojis if the user explicitly requests it.
- file_path accepts absolute host paths or project-relative paths.

Output: "File written: {path} ({N} chars)" on success, or an "Error: ..." string on failure."""
