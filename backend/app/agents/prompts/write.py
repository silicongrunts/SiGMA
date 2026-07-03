"""
Prompt for the write tool.
"""

PROMPT = """Writes a file to the local filesystem.

Usage:
- This tool overwrites the existing file if there is one at the provided path.
- If this is an existing file, you MUST use the read tool first earlier in this conversation. The tool will fail if the file has not been read or has been modified on disk since the last read. A compaction resets this state — re-read after a compact before writing.
- Prefer the edit tool for modifying existing files — it only sends the diff. Only use this tool to create new files or for complete rewrites.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- NEVER create documentation files (*.md) or README files unless explicitly requested by the User.
- Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.
- file_path accepts absolute host paths or project-relative paths.

Output: "File written: {path} ({N} chars)" on success, or an "Error: ..." string on failure."""
