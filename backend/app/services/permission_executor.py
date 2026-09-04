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
(``POST /chat/stream`` with ``resume=true``). The pending request is durable
because it lives in the persisted ``interaction_state`` column of the task
row: a process restart or page refresh cannot lose it — the row is the
source of truth, and the resume path re-reads it from the database.
"""

import hashlib
from typing import Any, Optional

from app.agents.tools.bash_permissions import check_bash_permission
from app.agents.tools.read_state import record_path_read
from app.agents.tools.registry import tool_registry
from app.agents.tools.schema_validation import validate_tool_args
from app.core.logging import get_logger
from app.core.text_diff import DIFF_LINE_SOFT_LIMIT, compute_diff_lines
from app.services.file_service import PathAccessLevel, file_service
from app.services.llm_loop_runner import LLMLoopRunner
from app.services.pauses import PermissionRequestPause

logger = get_logger(__name__)


# The four permission categories. Tools map onto one of these (see
# ``_check_permission``); everything else is either read-only or explicitly
# exempt. These values are persisted as ``auto_approve.<category>`` config keys
# and sent to the frontend as the ``tool`` field of permission requests, so the
# auto-approve toggle matching is automatic.
PERMISSION_CATEGORIES: tuple[str, ...] = (
    "file_external", "file_internal", "bash", "notebook",
)

# Categories whose approval carries a resolved-target snapshot. For these an
# empty snapshot means the target could not be resolved at approval time, so
# the resume path must refuse execution instead of running blind.
FILE_PERMISSION_CATEGORIES: tuple[str, ...] = ("file_external", "file_internal")


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

    # Structural pre-check: reject malformed arguments before any approval gate.
    # A doomed call (missing/typo'd param) must never surface a permission
    # dialog — the user would approve, then the tool would error anyway.
    schema_error = validate_tool_args(tool_def.input_schema, tool_args)
    if schema_error:
        return schema_error

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
    # Deterministic preflight: a tool may declare checks that are guaranteed to
    # fail at execution time (must-read-first contract, edit's old==new
    # invariant). Run them *before* the category-specific gate so a doomed call
    # never surfaces an approval dialog — the user would approve, then the tool
    # would error anyway. The preflight receives the same kwargs the runner
    # injected (project_id/session_id); None means "proceed to the gate".
    if tool_def.preflight is not None:
        preflight_error = await tool_def.preflight(**tool_args)
        if preflight_error:
            return preflight_error

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


def write_target_arg(tool_args: dict) -> str:
    """The filesystem path a write-category tool targets.

    Shared by the approval gate and the resume-side target-drift check so
    both extract — and therefore resolve — the same argument.
    """
    return (
        tool_args.get("file_path") or tool_args.get("path")
        or tool_args.get("notebook_path") or ""
    )


def approved_target_drift_error(
    project_id: str, approved_resolved: str, tool_args: dict, category: str = "",
) -> str:
    """Return the not-executed error when the approved write target drifted.

    Re-resolves the tool's target path and compares it with the snapshot
    taken when the approval dialog was shown. Returns ``""`` when the target
    is unchanged. A file-category approval whose snapshot is empty (the
    target could not be resolved at approval time) is refused like a drift —
    an unresolvable target must be re-requested, never executed against an
    unvalidated path. bash / notebook categories carry no snapshot and
    always pass.
    """
    if not approved_resolved:
        if category in FILE_PERMISSION_CATEGORIES:
            return (
                "Error: the approved target could not be re-resolved; "
                "the operation was not executed. Re-request the operation "
                "with a resolvable path."
            )
        return ""
    current = file_service.resolve_write_target(
        project_id, write_target_arg(tool_args),
    )
    current_str = str(current) if current is not None else "<unresolvable>"
    if current_str == approved_resolved:
        return ""
    return (
        "Error: the approved target changed since approval "
        f"(approved target: {approved_resolved}; "
        f"current target: {current_str}). "
        "The operation was not executed. Ask the user to approve the "
        "current target if it is still needed."
    )


_NOTEBOOK_DRIFT_TAIL = (
    "The operation was not executed. Re-request the cell execution "
    "if it is still needed."
)


async def approved_notebook_drift_error(
    project_id: str, tool_args: dict, approved_sha256: str,
) -> str:
    """Return the not-executed error when the approved notebook cell changed.

    A notebook approval snapshots the sha256 of the cell's full source —
    hashed before the dialog's CONTENT_SOFT_LIMIT truncation, so the digest
    always describes what the user reviewed. On resume the current source is
    re-hashed and compared, so a cell edited while the task was parked cannot
    execute under an approval granted for the old code. Checkpoints saved
    before the snapshot existed carry no hash and pass unchanged.
    """
    if not approved_sha256:
        return ""
    notebook_path = tool_args.get("notebook_path", "")
    cell_id = tool_args.get("cell_id", "")
    if not notebook_path or not cell_id:
        return ""
    from app.agents.tools.notebook_utils import (
        cell_source_text, find_cell_index, read_notebook_json,
    )
    try:
        notebook, _location = await read_notebook_json(notebook_path, project_id)
    except Exception:
        # The tool re-reads the notebook itself and will surface the read
        # failure as its own error result; there is nothing extra to refuse.
        logger.warning(
            "Failed to re-read notebook %s to verify the approved cell",
            notebook_path, exc_info=True,
        )
        return ""
    idx = find_cell_index(notebook.get("cells", []), cell_id)
    if idx < 0:
        return (
            f"Error: the approved notebook cell was removed since approval "
            f"(cell {cell_id}). {_NOTEBOOK_DRIFT_TAIL}"
        )
    current = hashlib.sha256(
        cell_source_text(notebook["cells"][idx]).encode("utf-8"),
    ).hexdigest()
    if current == approved_sha256:
        return ""
    return (
        f"Error: the approved notebook cell changed since approval "
        f"(cell {cell_id} no longer matches the approved content). "
        f"{_NOTEBOOK_DRIFT_TAIL}"
    )


# Tools whose bodies enforce the must-read-first contract against the
# in-memory per-session read-state cache (read_state.py). An approval pause
# for one of these can only be raised after the tool's preflight must-read
# check passed, so the model had read the target when the approval was
# granted; the cache itself is process-local and does not survive the restart
# that makes an approval resume necessary.
_MUST_READ_TOOLS: frozenset[str] = frozenset({
    "write", "edit", "notebook_edit", "notebook_run_cell",
})


def restore_approved_read_state(
    tool_name: str, tool_args: dict, project_id: str, session_id: str,
) -> None:
    """Re-record the approved target as read before the approved execution.

    Without this, a write/edit/notebook approval resumed after a process
    restart would be rejected by the tool's own must-read check — the user's
    approval consumed with nothing executed. A pause implies the target was
    read (or did not yet exist) under this session, so re-recording it
    restores the exact cache state the approval was granted under. Scoped to
    the approved target and session only: every other file keeps requiring
    its own prior read, including after the resume.
    """
    if tool_name not in _MUST_READ_TOOLS or not session_id:
        return
    target = write_target_arg(tool_args)
    if not target:
        return
    if tool_name.startswith("notebook"):
        from app.agents.tools.notebook_utils import (
            NotebookToolError, normalize_notebook_path,
        )
        try:
            resolved = normalize_notebook_path(target, project_id).absolute_path
        except NotebookToolError:
            return
    else:
        resolved = file_service.resolve_write_target(project_id, target)
        if resolved is None:
            return
    record_path_read(session_id, resolved, content="", is_partial=False)


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
        content=result.content or command,
        description=description,
    )


async def _check_notebook_run(
    tool_args: dict, project_id: str,
) -> Optional[str]:
    """Require approval to execute a notebook cell. The cell's source code is
    shown in the approval dialog as preview content, and the sha256 of the
    full source is snapshotted so the resume path can refuse to execute a
    cell that changed while the task was parked."""
    notebook_path = tool_args.get("notebook_path", "")
    cell_id = tool_args.get("cell_id", "")
    code = ""
    source_sha256 = ""
    if notebook_path and cell_id:
        try:
            from app.agents.tools.notebook_utils import (
                NotebookToolError, cell_source_text, find_cell_index, read_notebook_json,
            )
            notebook, _location = await read_notebook_json(notebook_path, project_id)
            cells = notebook.get("cells", [])
            idx = find_cell_index(cells, cell_id)
            if idx < 0:
                return f"Error: Cell not found: {cell_id}"
            cell = cells[idx]
            if cell.get("cell_type") != "code":
                return f"Error: Cell {cell_id} is not a code cell."
            code = cell_source_text(cell)
            source_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        except NotebookToolError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            logger.warning(
                "Failed to read notebook cell %s for permission prompt",
                cell_id, exc_info=True,
            )
            return f"Error: Unable to read cell {cell_id} for approval: {exc}"

    # Auto-approve short-circuits before pausing.
    if await _is_auto_approved(project_id, "notebook"):
        return None

    raise PermissionRequestPause(
        tool="notebook",
        tool_name="notebook_run_cell",
        path=notebook_path,
        operation="execute code in",
        content=code or f"(cell {cell_id})",
        content_sha256=source_sha256,
    )


async def _check_write(
    tool_name: str,
    tool_args: dict,
    project_id: str,
) -> Optional[str]:
    """Require approval for filesystem writes, classifying the target into the
    ``file_internal`` (sandbox /tmp) or ``file_external`` category.

    The checkpoint carries a resolved-target snapshot (``resolved_path``) so
    the resume path can detect a target that moved while the task was parked.

    For the ``edit`` tool, the before/after strings are composed into the preview.
    """
    target_path = write_target_arg(tool_args)
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
    diff_lines = None
    diff_truncated = False
    if not content and tool_name == "edit":
        old = tool_args.get("old_string", "")
        new = tool_args.get("new_string", "")
        if old or new:
            content = (
                f"--- before ---\n{old}\n--- after ---\n{new}"
                if old != new else old
            )
            # Diff the replacement fragment itself (old_string -> new_string),
            # not the whole file — that is what the user is approving. Truncate
            # at the soft limit to bound payload size.
            diff_lines = compute_diff_lines(old, new)
            if len(diff_lines) > DIFF_LINE_SOFT_LIMIT:
                diff_lines = diff_lines[:DIFF_LINE_SOFT_LIMIT]
                diff_truncated = True

    # Snapshot the resolved target at approval time; the resume path
    # re-resolves and compares against it (approved_target_drift_error). An
    # empty snapshot (unresolvable target) is persisted as-is: the dialog
    # still shows the requested path, and the resume path refuses execution
    # for file categories so the operation is re-requested with a resolvable
    # target instead of running blind.
    resolved = file_service.resolve_write_target(project_id, target_path)

    raise PermissionRequestPause(
        tool=category,
        tool_name=tool_name,
        path=target_path,
        operation=operation,
        content=content or "",
        diff_lines=diff_lines,
        diff_truncated=diff_truncated,
        resolved_path=str(resolved) if resolved is not None else "",
    )
