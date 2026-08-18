"""
Task tool prompts — create, update, list, get, and bulk-replace tasks.

Field-level descriptions live in each tool's input_schema; these prompts
cover behavioral context, workflow rules, and output shape only — same
convention as annotation.py and library.py.
"""

PROMPT_TASK_CREATE = """Create a task to track progress. Use for multi-step work (3+ distinct steps), when the user asks for a todo list, or when the user provides a numbered or comma-separated list of tasks.

Output: "Task created: [{id}] {subject}"."""

PROMPT_TASK_UPDATE = """Update a task's status or details.

- Status flow: pending → in_progress → completed. Set status to deleted to remove a task.
- Mark in_progress before starting work and completed immediately after finishing. Only mark completed when the work is fully done (tests passing, no unresolved errors, nothing partial).
- When you complete the last remaining task, all tasks are automatically cleared — you do not need to delete them.
- metadata replaces the existing value entirely — pass the complete metadata, not a delta.
- Work tasks in ID order when multiple are available.

Output: "Task updated: [{id}] {subject} (status={status})", plus "All tasks completed — {N} task(s) cleared." when completing the last task triggers the auto-clear."""

PROMPT_TASK_LIST = """List the session's active tasks, sorted pending first, then in_progress, then completed.

Output: "[{id}] {subject} ({status})" per line; "No tasks." when the list is empty."""

PROMPT_TASK_GET = """Get one task's full details.

Output: "[{id}] {subject}", "Status: {status}", "Description: {description, or (none) when empty}"."""

PROMPT_TASK_WRITE = """Replace the session's entire task list in one call; omitted tasks are removed. Use to create the initial breakdown of a multi-step task and to restructure the list as work evolves. Skip it for single trivial tasks.

Each item needs content and status (pending/in_progress/completed), optionally description. Do not announce list updates to the user — just make them. When 3 or more submitted items are marked completed and none of their text contains "verif", the output appends a reminder to verify the work before considering it done.

Output: "Todo list replaced with {N} items:" followed by "[{id}] {subject} ({status})" per item."""
