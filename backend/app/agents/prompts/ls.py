"""
Prompt for the ls tool.
"""

PROMPT = """List a directory.

Usage:
- Empty `dirname` returns a recursive tree of the project root (depth-limited).
- Relative `dirname` (e.g. "src/components") lists the immediate children of that directory inside the project.
- Absolute `dirname` (e.g. "/home/user") browses the host filesystem directly (read-only) — useful when the user references paths outside the project.
- Hidden entries (starting with ".") are filtered out. Directories are suffixed with "/".

Output: entries one per line (tree mode indents by depth); "(empty directory)" / "(empty project)" when there is nothing to list; "Directory not found: {dirname}" when the path does not exist; a "Not a directory" error when it is not a directory."""
