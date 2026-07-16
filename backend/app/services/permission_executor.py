"""Permission-aware tool executor.

Centralizes approval flow for the four permission categories:

- ``file_external``: writes/edits to paths outside the project sandbox and /tmp.
- ``file_internal``: writes/edits to paths inside the project sandbox or /tmp.
- ``bash``: non-read-only shell commands.
- ``notebook``: executing code in a notebook cell.

Read-only tools and the annotation/library/draw/task families bypass approval
entirely (see ``_EXEMPT_TOOLS`` and the ``is_read_only`` flag).

Auto-approve: each category's auto-approve flag is read live from the
per-project ``project_config`` table (key ``auto_approve.<category>``). When the
flag is on, the category is approved silently — no pause is needed. Reads are
uncached (single-user local SQLite is cheap enough) so a settings change takes
effect on the next tool call.

When approval is needed, ``execute_with_permission`` raises
``PermissionRequestPause``. The LLM loop runner catches it, parks the task as
``awaiting_input`` (same mechanism used by interactive tools like
``ask_user_question``), and the user's response arrives via the resume path
(``POST /chat/stream`` with ``resume=true``). This makes permission pauses
crash-safe: a worker restart or page refresh does not lose the pending request,
because it is persisted in ``interaction_state`` rather than held in process
memory.
"""

from typing import Any, Optional

from app.agents.tools.bash_permissions import check_bash_permission
from app.agents.tools.registry import tool_registry
from app.core.logging import get_logger
from app.services.file_service import PathAccessLevel, file_service
from app.services.llm_loop_runner import LLMLoopRunner

logger = get_logger(__name__)


class PermissionRequestPause(Exception):
    """Raised when a tool needs user approval before it can execute.

    Carries the full context needed to render the approval dialog and to resume
    the task after the user responds. The LLM loop runner catches this, persists
    a checkpoint via ``mark_awaiting_input``, and emits an ``awaiting_input``
    SSE event. The user's response flows back through the resume path
    (``QueryLoop._resume_from_permission``).
    """

    def __init__(
        self,
        *,
        tool: str,
        tool_name: str = "",
        path: str = "",
        operation: str = "",
        content: str = "",
        description: str = "",
    ):
        self.tool = tool              # category: file_external/file_internal/bash/notebook
        self.tool_name = tool_name    # concrete tool invoked: write/edit/bash/...
        self.path = path
        self.operation = operation
        self.content = content
        self.description = description
        # Set by the runner when the pause propagates out of a subagent, so
        # the parent loop knows which agent tool_call to attach the result to.
        self.parent_tool_call_id = ""
        # Enriched by agent_service when the pause escapes a subagent, so the
        # checkpoint can be saved as a subagent interaction and the subagent
        # resumed mid-loop after the user responds (same pattern as
        # InteractiveToolPause). Empty for direct (main-loop) tool pauses.
        self.agent_session_id = ""
        self.agent_type = ""
        self.agent_usage_baseline: dict | None = None
        self.inner_tool_call_id = ""
        # The full tool_args of the paused tool call. Set by the runner when it
        # catches the pause (the runner has the LLM-produced args). Needed to
        # re-execute the tool on resume after user approval.
        self.tool_args: dict = {}
        super().__init__(f"Permission required for {tool_name or tool}: {operation} {path}")


# The four permission categories. Tools map onto one of these (see
# ``_check_permission``); everything else is either read-only or explicitly
# exempt. These values are persisted as ``auto_approve.<category>`` config keys
# and sent to the frontend as the ``tool`` field of permission requests, so the
# auto-approve toggle matching is automatic.
PERMISSION_CATEGORIES: tuple[str, ...] = (
    "file_external", "file_internal", "bash", "notebook",
)


async def execute_with_permission(
    tool_name: str,
    tool_args: dict,
    tool_def: Any = None,
    *,
    project_id: str,
) -> str:
    """Dispatch ``tool_name`` after running the appropriate permission gate.

    Returns the tool's output string on success, or a denial / error message
    string if permission is denied (the caller treats both as the tool result
    that goes back to the LLM). Raises ``PermissionRequestPause`` when user
    approval is needed — the caller (LLM loop runner) catches it and parks the
    task as ``awaiting_input``.
    """
    if tool_def is None:
        tool_def = tool_registry.get(tool_name)
    if tool_def is None:
        return f"Error: Unknown tool '{tool_name}'"
    if tool_def.call is None:
        return f"Error: Tool '{tool_name}' has no call implementation"

    denied = await _check_permission(
        tool_name, tool_args, tool_def, project_id,
    )
    if denied:
        return denied
    return await LLMLoopRunner.call_tool(tool_def, tool_args)


async def _check_permission(
    tool_name: str,
    tool_args: dict,
    tool_def: Any,
    project_id: str,
) -> Optional[str]:
    """Returns denial string if denied, None to proceed with tool execution.

    Raises ``PermissionRequestPause`` when user approval is needed and the
    category is not auto-approved.
    """
    if tool_name == "bash":
        return await _check_bash(tool_args, project_id)
    if tool_name == "notebook_run_cell":
        return await _check_notebook_run(tool_args, project_id)
    if tool_name in _EXEMPT_TOOLS:
        return None
    if not tool_def.is_read_only:
        return await _check_write(tool_name, tool_args, project_id)
    return None


# Tools that modify project database rows or generated resources rather than the
# filesystem, plus annotation tools whose path containment is enforced by the
# tool layer. They bypass the write-approval dialog. See RULES/SECURITY.md for
# the rationale (single-user local app; these do not touch host paths).
_EXEMPT_TOOLS: frozenset[str] = frozenset({
    # annotation_* read the file via safe_join and store rows in the project DB
    "annotation_new", "annotation_rm", "annotation_get",
    "annotation_reply", "annotation_list",
    # library_* mutate the project knowledge base (project DB), not host files
    "library_new", "library_mkdir", "library_mv", "library_update", "library_rm",
    # draw_image generates an image into the project, task_* mutate task rows
    "draw_image", "task_create", "task_update", "task_write",
})


async def _is_auto_approved(project_id: str, category: str) -> bool:
    """Read ``auto_approve.<category>`` live from the project DB.

    Uncached by design (see module docstring). Any read failure is treated as
    "not auto-approved" so the safer approval-dialog path is taken.
    """
    from app.database.unit_of_work import UnitOfWork
    if not project_id:
        return False
    try:
        async with UnitOfWork(project_id) as uow:
            val = await uow.config.get(f"auto_approve.{category}", "false")
    except Exception:
        logger.debug(
            "Failed to read auto_approve.%s for project %s", category, project_id,
            exc_info=True,
        )
        return False
    return val == "true"


async def _check_bash(
    tool_args: dict, project_id: str,
) -> Optional[str]:
    """Route bash through the read-only allowlist / approval flow.

    The synchronous classifier decides whether approval is needed. If it is,
    the ``bash`` auto-approve flag is consulted first; only when it is off (or
    unreadable) does the method raise ``PermissionRequestPause``.
    """
    command = tool_args.get("command", "")
    if not command:
        return None
    description = tool_args.get("description", "")

    result = check_bash_permission(command)
    if result.approved:
        return None  # read-only command — no approval needed

    # Non-read-only command. Check auto-approve before pausing.
    if await _is_auto_approved(project_id, "bash"):
        return None

    raise PermissionRequestPause(
        tool="bash",
        tool_name="bash",
        path=result.path,
        operation=result.operation or "execute",
        content=result.content or command[:800],
        description=description,
    )


async def _check_notebook_run(
    tool_args: dict, project_id: str,
) -> Optional[str]:
    """Require approval to execute a notebook cell. The cell's source code is
    shown in the approval dialog as preview content."""
    notebook_path = tool_args.get("notebook_path", "")
    cell_id = tool_args.get("cell_id", "")
    code = ""
    if notebook_path and cell_id:
        try:
            from app.agents.tools.notebook_utils import (
                NotebookToolError, cell_source_text, find_cell_index, read_notebook_json,
            )
            notebook, _location = await read_notebook_json(notebook_path, project_id)
            cells = notebook.get("cells", [])
            idx = find_cell_index(cells, cell_id)
            if idx == -2:
                return f"Error: Cell ID '{cell_id}' matches multiple cells. Provide more characters for a unique match."
            if idx < 0:
                return f"Error: Cell not found: {cell_id}"
            cell = cells[idx]
            if cell.get("cell_type") != "code":
                return f"Error: Cell {cell_id} is not a code cell."
            code = cell_source_text(cell)
        except NotebookToolError as exc:
            return f"Error: {exc}"
        except Exception:
            logger.debug(
                "Failed to read notebook cell source for permission prompt",
                exc_info=True,
            )

    # Auto-approve short-circuits before pausing.
    if await _is_auto_approved(project_id, "notebook"):
        return None

    raise PermissionRequestPause(
        tool="notebook",
        tool_name="notebook_run_cell",
        path=notebook_path,
        operation="execute code in",
        content=code[:2000] if code else f"(cell {cell_id})",
    )


async def _check_write(
    tool_name: str,
    tool_args: dict,
    project_id: str,
) -> Optional[str]:
    """Require approval for filesystem writes, classifying the target into the
    ``file_internal`` (sandbox /tmp) or ``file_external`` category.

    For the ``edit`` tool, the before/after strings are composed into the preview.
    """
    target_path = (
        tool_args.get("file_path") or tool_args.get("path")
        or tool_args.get("notebook_path") or ""
    )
    if not target_path:
        return None

    level = file_service.check_write_allowed(project_id, target_path)

    is_internal = level in (PathAccessLevel.SANDBOX, PathAccessLevel.TMP)
    category = "file_internal" if is_internal else "file_external"

    # Auto-approve short-circuits before pausing.
    if await _is_auto_approved(project_id, category):
        return None

    operation = {
        "write": "write to", "edit": "edit",
        "notebook_edit": "edit",
    }.get(tool_name, "modify")

    content = tool_args.get("content", "")
    if not content and tool_name == "edit":
        old = tool_args.get("old_string", "")
        new = tool_args.get("new_string", "")
        if old or new:
            content = (
                f"--- before ---\n{old}\n--- after ---\n{new}"
                if old != new else old
            )

    raise PermissionRequestPause(
        tool=category,
        tool_name=tool_name,
        path=target_path,
        operation=operation,
        content=content[:2000] if content else "",
    )
