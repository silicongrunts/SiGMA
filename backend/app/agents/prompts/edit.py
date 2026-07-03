"""
Prompt for the edit tool.
"""

PROMPT = """Performs exact string replacements in files.

Usage:
- You must use your read tool at least once earlier in this conversation before editing. This tool will error if the file has not been read or has been modified on disk since the last read. A compaction resets this state — re-read after a compact before editing.
- When matching text, ensure you reproduce it exactly (whitespace, indentation, etc.). The match is a literal substring search, not a regex.
- The edit will FAIL if old_string is not unique in the file. Either provide a larger string with more surrounding context to make it unique, or use replace_all to change every instance of old_string.
- Use replace_all for replacing and renaming strings across the file. This parameter is useful if you want to rename a variable for instance.
- old_string and new_string must differ; identical values produce an error.
- file_path accepts absolute host paths or project-relative paths.

Output: "File edited: {path} ({N} replacement(s))" on success, or an "Error: ..." string on failure (file not found, old_string missing or non-unique, must-read-first violated, etc.)."""
