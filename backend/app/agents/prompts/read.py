"""
Prompt for the read tool.
"""

PROMPT = """Reads a file from the local filesystem. Supports plain text, PDF, and PNG/JPG images.

Usage:
- file_path accepts absolute host paths or project-relative paths.
- By default, reads up to 200 lines starting from the beginning of the file. Pass offset=0 (or omit) to start at the first line.
- offset is 0-indexed and must be >=0. limit caps lines: omit or pass 0 for 200; negative returns the last abs(limit) lines.
- If the default cap is applied and the file has more lines, a "Showing lines X-Y of Z (N more not shown)" suffix is appended so you know which range you see and to paginate with offset.
- PDF files (.pdf) are converted to markdown automatically; offset/limit apply to the converted text. Conversion is cached per session, so subsequent reads of the same PDF are fast (and stay cached unless the file changes on disk).
- PNG/JPG image files may be returned directly for visual inspection when the current model context supports images. Otherwise, read returns the image path and you must use vision_analyze to inspect it.
- Binary files (other than supported image/PDF formats) return an error.
- This tool only reads files. To list a directory, use the ls tool.

Output: the file content as plain text on success, or an "Error: ..." string on failure (file not found, binary, etc.)."""
