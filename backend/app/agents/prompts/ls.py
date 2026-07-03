"""
Prompt for the ls tool.
"""

PROMPT = """List files and directories.

Usage:
- Empty `dirname` returns a recursive tree of the project root (depth-limited).
- Relative `dirname` (e.g. "src/components") returns the immediate children of that sandbox directory.
- Absolute `dirname` (e.g. "/home/user") browses the host filesystem directly (read-only) — useful when the user references paths outside the project.
- Hidden entries (those starting with ".") are filtered out.
- Directories are suffixed with "/".

Output: one entry per line on success; "(empty directory)" / "(empty project)" if nothing to list; or an "Error: ..." string if the path does not exist or is not a directory."""
