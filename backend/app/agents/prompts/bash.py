"""
Prompt for the bash tool.
"""

PROMPT = """Executes a given bash command and returns its output.

The working directory is always the project root. Shell state (environment variables, cd, etc.) does not persist between commands — use absolute paths instead.

IMPORTANT: Avoid using this tool to run find, grep, cat, head, tail, sed, awk, or echo commands, unless explicitly instructed or after you have verified that a dedicated tool cannot accomplish your task. Instead, use the appropriate dedicated tool as this will provide a much better experience for the user:
 - File search: Use glob (NOT find or ls)
 - Content search: Use grep (NOT grep or rg)
 - Read files: Use read (NOT cat/head/tail)
 - Edit files: Use edit (NOT sed/awk)
 - Write files: Use write (NOT echo >/cat <<EOF)
 - Communication: Output text directly (NOT echo/printf)

# Instructions
- Always quote file paths that contain spaces with double quotes in your command
- Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of cd.
- `timeout` is seconds: default 120, max 600.
- Always provide a `description`: one short sentence stating what the command does and noting any risk or side effect (e.g. deletes files, writes outside the project, mutates git state). The user sees this when approving non-read-only commands.
- When issuing multiple commands:
  - If the commands are independent and can run in parallel, make multiple bash tool calls in a single message.
  - If the commands depend on each other and must run sequentially, use a single bash call with && to chain them together.
  - Use ; only when you need to run commands sequentially but don't care if earlier commands fail.
  - DO NOT use newlines to separate commands (newlines are ok in quoted strings).
- Avoid unnecessary sleep commands.
"""
