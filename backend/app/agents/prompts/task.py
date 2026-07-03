"""
Task tool prompts — create, update, list, get, and bulk-replace tasks.

Field-level descriptions live in each tool's input_schema; these prompts
cover behavioral context, workflow rules, and output shape only — same
convention as annotation.py and library.py.
"""

PROMPT_TASK_CREATE = """Create a new task to track progress in the current coding session.

### When to Use
- Complex multi-step tasks — 3+ distinct steps or actions
- Non-trivial and complex tasks — tasks that require careful planning
- User explicitly requests todo list
- User provides multiple tasks (numbered or comma-separated)
- After receiving new instructions — immediately capture requirements as tasks
- When you start working on a task — mark it as in_progress BEFORE beginning work

### Tips
- Create tasks with clear, specific subjects that describe the outcome"""

PROMPT_TASK_UPDATE = """Update a task's status or details.

### When to Use
- When you have completed the work described in a task — mark it as completed
- When a task is no longer needed — set status to deleted
- When you start working — mark it as in_progress BEFORE beginning
- When requirements change or become clearer — update description

### Status Workflow
Status progresses: pending → in_progress → completed
Use deleted to permanently remove a task.

### Important Rules
- ONLY mark a task as completed when you have FULLY accomplished it
- Never mark a task as completed if tests are failing, implementation is partial, you encountered unresolved errors, or you couldn't find necessary files/dependencies
- After completing a task, use task_list to find your next task
- Prefer working on tasks in ID order (lowest ID first) when multiple tasks are available
- When you mark the last remaining task as completed, all tasks are automatically cleared (soft-deleted) — you do NOT need to delete them manually. Simply proceed with the conversation or create new tasks if needed.
- metadata replaces any existing value entirely — pass the complete metadata you want stored, not a delta."""

PROMPT_TASK_LIST = """List all tasks in the current task list.

### When to Use
- To see what tasks are available to work on
- To check overall progress on the project
- After completing a task, to check for next available work
- Prefer working on tasks in ID order (lowest ID first) when multiple tasks are available"""

PROMPT_TASK_GET = """Get full details and context for a specific task.

### When to Use
- When you need the full description and context before starting work on a task
- After being assigned a task, to get complete requirements

### Output
Returns subject, status, and description (or "(none)" if description is empty)."""

PROMPT_TASK_WRITE = """Replace the entire structured task list for this session.
Use this tool to break down complex tasks into manageable steps and track your progress.

This is full replacement: omitted existing tasks are removed.

Note: Other than when first creating todos, don't tell the user you're updating todos, just do it.

### When to Use This Tool
Use proactively for:
1. Complex multi-step tasks — 3+ distinct steps or actions
2. Non-trivial and complex tasks — tasks that require careful planning or multiple operations
3. User explicitly requests todo list
4. User provides multiple tasks (numbered or comma-separated)
5. After receiving new instructions — immediately capture requirements as tasks
6. When you start working on a task — mark it as in_progress BEFORE beginning work
7. After completing a task — mark it as completed and add new follow-up tasks discovered during implementation

### When NOT to Use
Skip using this tool when:
- There is only a single, straightforward task
- The task is trivial and tracking it provides no organizational benefit
- The task can be completed in less than 3 trivial steps
- The task is purely conversational or informational

### Task States and Management
1. Status flow: pending → in_progress → completed (or deleted)
2. Task breakdown: Create specific, actionable items. Break complex work into smaller, manageable pieces.
3. Mark in_progress BEFORE starting: Always mark a task as in_progress before beginning to work on it.
4. Mark completed immediately after finishing: Don't batch up completed tasks.
5. Add new tasks as they arise: If you discover new work, add it to the list.
6. Delete irrelevant tasks: Remove tasks that are no longer needed.

### Verification Nudge
When you complete 3+ tasks and none of their content includes "verif" or "verify", the tool output will include a reminder to verify your work before considering it done."""
