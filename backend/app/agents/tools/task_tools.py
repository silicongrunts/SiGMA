"""
Task management tools — task_create, task_update, task_list, task_get, task_write.

All tasks are persisted to the project database via task_service.
task_write does bulk replacement; the task_* tools do individual CRUD.
"""

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.prompts import (
    PROMPT_TASK_CREATE, PROMPT_TASK_UPDATE, PROMPT_TASK_LIST, PROMPT_TASK_GET,
    PROMPT_TASK_WRITE,
)
from app.services.task_service import task_service


async def _task_create(
    subject: str,
    description: str = "",
    project_id: str = "",
    session_id: str = "",
    metadata: dict | None = None,
) -> str:
    """Create a new task. Returns the task ID and subject."""
    if not subject or not subject.strip():
        return "Error: subject must not be empty"
    task = await task_service.create_task(
        project_id=project_id,
        session_id=session_id,
        subject=subject,
        description=description,
        metadata_json=metadata,
    )
    result = f"Task created: [{task.id}] {task.subject}"
    return result


async def _task_update(
    id: str,
    status: str = "",
    subject: str = "",
    description: str = "",
    project_id: str = "",
    session_id: str = "",
    metadata: dict | None = None,
) -> str:
    """Update a task."""
    task, err = await task_service.resolve_task(project_id, session_id, id)
    if err:
        return f"Error: {err}"
    resolved_id = task.id

    if status and status not in ("pending", "in_progress", "completed", "deleted"):
        return f"Error: Invalid status '{status}'"

    fields = {}
    if status:
        fields["status"] = status
    if subject:
        fields["subject"] = subject
    if description:
        fields["description"] = description
    if metadata is not None:
        fields["metadata_json"] = metadata

    updated = await task_service.update_task(project_id, resolved_id, **fields)
    cleaned_count = 0
    if fields.get("status") == "completed" and session_id:
        cleaned_count = await task_service.cleanup_completed_tasks(project_id, session_id)
    result = f"Task updated: [{updated.id}] {updated.subject} (status={updated.status})"
    if cleaned_count:
        result += f"\nAll tasks completed — {cleaned_count} task(s) cleared."
    return result


async def _task_list(project_id: str = "", session_id: str = "") -> str:
    """List all active tasks."""
    tasks = await task_service.list_active_tasks(project_id, session_id)
    if not tasks:
        return "No tasks."

    lines = []
    for t in sorted(tasks, key=lambda x: {"pending": 0, "in_progress": 1, "completed": 2, "deleted": 3}.get(x.status, 99)):
        lines.append(f"[{t.id}] {t.subject} ({t.status})")
    return "\n".join(lines)


async def _task_get(id: str, project_id: str = "", session_id: str = "") -> str:
    """Get full details of a task."""
    task, err = await task_service.resolve_task(project_id, session_id, id)
    if err:
        return f"Error: {err}"

    return (
        f"[{task.id}] {task.subject}\n"
        f"Status: {task.status}\n"
        f"Description: {task.description or '(none)'}"
    )


# ── Register ──

tool_registry.register(ToolDefinition(
    name="task_create",
    description="Create a new task to track progress. Use for breaking down complex work into steps.",
    prompt=PROMPT_TASK_CREATE,
    input_schema={
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "A brief title for the task"},
            "description": {"type": "string", "description": "What needs to be done", "default": ""},
            "metadata": {"type": "object", "description": "Arbitrary metadata to attach to the task"},
        },
        "required": ["subject"],
    },
    call=lambda subject, description="", metadata=None, project_id="", session_id="": _task_create(
        subject, description, project_id, session_id, metadata,
    ),
    requires_project_id=True,
    requires_session_id=True,
))

tool_registry.register(ToolDefinition(
    name="task_update",
    description="Update a task's status or details. Use to mark progress (pending→in_progress→completed).",
    prompt=PROMPT_TASK_UPDATE,
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The ID of the task to update (8+ chars for prefix match)"},
            "status": {"type": "string", "description": "New status: pending, in_progress, completed, deleted", "default": ""},
            "subject": {"type": "string", "description": "New subject for the task", "default": ""},
            "description": {"type": "string", "description": "New description", "default": ""},
            "metadata": {"type": "object", "description": "New metadata; replaces any existing metadata entirely"},
        },
        "required": ["id"],
    },
    call=lambda id, status="", subject="", description="", metadata=None, project_id="", session_id="": _task_update(
        id, status, subject, description, project_id, session_id, metadata,
    ),
    requires_project_id=True,
    requires_session_id=True,
))

tool_registry.register(ToolDefinition(
    name="task_list",
    description="List all tasks with their status.",
    prompt=PROMPT_TASK_LIST,
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    call=lambda project_id="", session_id="": _task_list(project_id, session_id),
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="task_get",
    description="Get full details of a specific task.",
    prompt=PROMPT_TASK_GET,
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The ID of the task to retrieve (8+ chars for prefix match)"},
        },
        "required": ["id"],
    },
    call=lambda id, project_id="", session_id="": _task_get(id, project_id, session_id),
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=True,
))


# ── task_write (bulk replace) ────────────────────────────────────────

async def _task_write(todos: list, project_id: str = "", session_id: str = "") -> str:
    """Replace the entire task list for a session."""
    if not session_id or not project_id:
        return "Error: project_id and session_id are required"

    items = []
    for todo in todos:
        content = todo.get("content", todo.get("subject", ""))
        if content:
            items.append(todo)

    created = await task_service.replace_tasks(project_id, session_id, items)

    # Build result with full task list so LLM can see IDs
    lines = [f"Todo list replaced with {len(created)} items:"]
    for t in created:
        lines.append(f"[{t.id}] {t.subject} ({t.status})")

    # Verification nudge
    completed = [t for t in todos if t.get("status") == "completed"]
    if (
        len(completed) >= 3
        and not any("verif" in (t.get("content", "") + t.get("subject", "")).lower() for t in completed)
    ):
        lines.append("\nNote: All items are marked complete. Consider verifying the work before considering it done.")

    return "\n".join(lines)


tool_registry.register(ToolDefinition(
    name="task_write",
    description="Replace the entire task list for the current session.",
    prompt=PROMPT_TASK_WRITE,
    input_schema={
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "A brief, actionable title in imperative form"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "Current status"},
                        "description": {"type": "string", "description": "Additional details about the task"},
                    },
                    "required": ["content", "status"],
                },
                "description": "Full replacement list; omitted existing tasks are removed.",
            },
        },
        "required": ["todos"],
    },
    call=lambda todos, project_id="", session_id="": _task_write(todos, project_id, session_id),
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=False,
))
