"""Permission-aware tool executor.

Centralizes approval flow for tools that modify state or execute code:
``bash``, ``notebook_run_cell``, and any non-read-only tool that targets a
path outside the project sandbox. Read-only tools bypass approval entirely.
"""

from typing import Any, Callable, Optional

from app.agents.tools.bash_permissions import check_bash_permission_async
from app.agents.tools.registry import tool_registry
from app.core.logging import get_logger
from app.services.file_service import PathAccessLevel, file_service
from app.services.llm_loop_runner import LLMLoopRunner

logger = get_logger(__name__)


async def execute_with_permission(
    tool_name: str,
    tool_args: dict,
    tool_def: Any = None,
    *,
    project_id: str,
    permission_requester: Optional[Callable] = None,
) -> str:
    """Dispatch ``tool_name`` after running the appropriate permission gate.

    Returns the tool's output string on success, or a denial / error message
    string if permission is denied (the caller treats both as the tool result
    that goes back to the LLM).
    """
    if tool_def is None:
        tool_def = tool_registry.get(tool_name)
    if tool_def is None:
        return f"Error: Unknown tool '{tool_name}'"
    if tool_def.call is None:
        return f"Error: Tool '{tool_name}' has no call implementation"

    denied = await _check_permission(
        tool_name, tool_args, tool_def, project_id, permission_requester,
    )
    if denied:
        return denied
    return await LLMLoopRunner.call_tool(tool_def, tool_args)


async def _check_permission(
    tool_name: str,
    tool_args: dict,
    tool_def: Any,
    project_id: str,
    requester: Optional[Callable],
) -> Optional[str]:
    """Returns denial string if denied, None to proceed with tool execution."""
    if tool_name == "bash":
        return await _check_bash(tool_args, requester)
    if tool_name == "notebook_run_cell":
        return await _check_notebook_run(tool_args, project_id, requester)
    if not tool_def.is_read_only:
        return await _check_write(tool_name, tool_args, project_id, requester)
    return None


async def _check_bash(tool_args: dict, requester: Optional[Callable]) -> Optional[str]:
    """Route bash through the read-only allowlist / approval flow."""
    command = tool_args.get("command", "")
    if not command:
        return None
    description = tool_args.get("description", "")
    result = await check_bash_permission_async(
        command=command,
        permission_requester=requester,
        description=description,
    )
    return None if result.approved else result.reason


async def _check_notebook_run(
    tool_args: dict, project_id: str, requester: Optional[Callable],
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

    if requester is None:
        return (
            "Permission denied: Cannot execute notebook code — "
            "user approval is required but the permission system is unavailable."
        )

    resp = await requester(
        tool="notebook_run_cell",
        path=notebook_path,
        operation="execute code in",
        content=code[:2000] if code else f"(cell {cell_id})",
    )
    if not resp.get("approved"):
        denial = f"User denied permission to execute code in notebook: {notebook_path}"
        reason = resp.get("reason", "")
        if reason:
            denial += f". User says: {reason}"
        return denial
    return None


async def _check_write(
    tool_name: str,
    tool_args: dict,
    project_id: str,
    requester: Optional[Callable],
) -> Optional[str]:
    """Require approval for writes outside the project sandbox. For the
    ``edit`` tool, the before/after strings are composed into the preview."""
    target_path = (
        tool_args.get("file_path") or tool_args.get("path")
        or tool_args.get("notebook_path") or ""
    )
    if not target_path:
        return None

    level = file_service.check_write_allowed(project_id, target_path)
    if level in (PathAccessLevel.SANDBOX, PathAccessLevel.TMP):
        return None  # Inside sandbox — no approval needed

    if requester is None:
        return (
            f"Permission denied: Cannot write to '{target_path}' — "
            "user approval is required but the permission system is unavailable."
        )

    operation = {
        "write": "write to", "edit": "edit",
        "notebook_edit": "edit", "notebook_run_cell": "execute code in",
        "annotation_new": "annotate", "annotation_reply": "annotate",
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

    resp = await requester(
        tool=tool_name,
        path=target_path,
        operation=operation,
        content=content[:2000] if content else "",
    )
    if not resp.get("approved"):
        denial = f"User denied permission to {operation} file: {target_path}"
        reason = resp.get("reason", "")
        if reason:
            denial += f". User says: {reason}"
        return denial
    return None
