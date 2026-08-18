"""
Prompt for the read tool.
"""

PROMPT = """Read a file from the local filesystem. Supports plain text, PDF, and PNG/JPG images.

Usage:
- file_path accepts absolute host paths or project-relative paths.
- offset is 0-indexed and must be >= 0. limit caps the lines returned (default 200; omit or pass 0 for the default); a negative limit returns the last abs(limit) lines (offset is ignored).
- Whenever the returned window stops short of EOF, a "Showing lines X-Y of Z (N more not shown)" suffix is appended — paginate with offset.
- Output uses `cat -n`-style line numbers (`N\\t<content>`); N is the file's real 1-indexed line number, usable directly in `sigma://synthesis/file?...&line=N` citations. The edit tool matches by literal string content — strip the `N\\t` prefix when constructing old_string.
- PDF files are converted to markdown; offset/limit apply to the converted text. The conversion is cached per session until the file changes on disk.
- PNG/JPG images are supported; how they are returned depends on the model context — see the appended note. Other binary files return an error.
- To list a directory, use the ls tool.

Output: the file content with `cat -n` line numbers on success, or an "Error: ..." string on failure (file not found, binary, etc.)."""
