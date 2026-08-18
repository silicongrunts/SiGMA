"""
Prompt for the bash tool.
"""

PROMPT = """Run a bash command and return its output.

The working directory is always the project root, and environment variables and other shell state do not persist between commands — use absolute paths. Commands execute via /bin/sh (POSIX shell); bash-only syntax such as [[ ]], arrays, or <<< is not guaranteed to work.

Prefer dedicated tools over shell commands: glob for finding files, grep for content search, read for reading files, edit for edits, write for writes, and plain response text instead of echo/printf. Use bash for these only when a dedicated tool cannot do the job.

Usage:
- Quote file paths containing spaces with double quotes.
- timeout is seconds: default 120, max 600.
- Always provide a `description`: one short sentence stating what the command does and any risk or side effect (e.g. deletes files, writes outside the project, mutates git state). The user sees it when approving non-read-only commands.
- Multiple commands: issue independent commands as separate parallel bash calls; chain dependent commands in one call with && (use ; only when later commands must run even if earlier ones fail). Do not separate commands with newlines (newlines inside quoted strings are fine).
- To wait, use the sleep tool, not bash sleep.

Output: "stdout: ..." / "-----" / "stderr: ..." / "-----" / "exit code: {N}"; on timeout the exit-code line notes the timeout. Any tool result longer than 50,000 characters is cut and marked "... [truncated]"."""
