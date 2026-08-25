"""
Prompt for the edit tool.
"""

PROMPT = """Replace exact strings in a file.

Usage:
- Read the file first. The tool fails if the file was never read or changed on disk since the last read; a compaction resets this state — re-read before editing.
- The read must cover the lines being edited: reads return a 200-line window by default, and an edit whose old_string falls outside every window read so far fails with the line range to read. replace_all additionally requires having read the whole file.
- The match is a literal substring search, not a regex: reproduce whitespace and indentation exactly.
- old_string must be non-empty and unique in the file. Provide more surrounding context to make it unique, or set replace_all to change every instance (e.g. renaming a variable). To create a file or replace its whole content, use the write tool.
- old_string and new_string must differ; identical values produce an error.
- file_path accepts absolute host paths or project-relative paths.
- Jupyter notebooks (.ipynb) are refused; use the notebook_edit tool.

Output: "File edited: {path} ({N} replacement(s))" on success, or an "Error: ..." string on failure (file not found, old_string missing or not unique, edit outside the lines read, must-read-first violated, etc.)."""
