"""
Notebook tools — read, edit, and execute Jupyter notebook cells.

3 tools: notebook_read, notebook_edit, notebook_run_cell.

All three share helpers in notebook_utils.py (path normalization, cell lookup,
XML formatting, Jupyter Contents-API read/save). The tools coordinate through
that shared layer; in particular `ensure_cell_ids` is invoked from the write
paths only, keeping `read_notebook_json` side-effect-free.
"""

import asyncio
from html import escape as _html_escape
import re as _re
import uuid as _uuid

from app.agents.tools.read_state import (
    record_path_read,
    verify_path_readable_fresh,
)
from app.agents.prompts import (
    PROMPT_NOTEBOOK_READ,
    PROMPT_NOTEBOOK_EDIT,
    PROMPT_NOTEBOOK_RUN_CELL,
)
from app.agents.tools.base import ToolDefinition
from app.agents.tools.notebook_utils import (
    NotebookToolError,
    cell_source_text,
    ensure_cell_ids,
    ensure_code_cell_fields,
    find_cell_index,
    format_cell_xml,
    format_output_xml,
    normalize_notebook_path,
    read_notebook_json,
    save_notebook_json,
    strip_execution_fields,
    to_jupyter_path,
)
from app.agents.tools.registry import tool_registry
from app.services.jupyter_service import get_jupyter


# ── Helpers ──────────────────────────────────────────────────────────

_ANSI_RE = _re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_DEFAULT_CELL_LIMIT = 5
_DEFAULT_OUTPUT_LIMIT = 200
_CELL_LIST_OUTPUT_LIMIT = 100


def _clean_traceback(lines: list | None) -> list[str]:
    return [_ANSI_RE.sub("", str(line)) for line in (lines or [])]


def _new_cell_id(cells: list) -> str:
    existing = {str(cell.get("id", "")) for cell in cells}
    while True:
        candidate = _uuid.uuid4().hex[:8]
        if candidate not in existing:
            return candidate


def _apply_cell_type(cell: dict, cell_type: str) -> None:
    if cell_type:
        cell["cell_type"] = cell_type
    if cell.get("cell_type") == "code":
        ensure_code_cell_fields(cell)
        cell["execution_count"] = None
        cell["outputs"] = []
    else:
        strip_execution_fields(cell)


def _format_run_result(result: dict, warning: str = "") -> str:
    """Format an execution result dict into a human-readable string."""
    status = result.get("status", "unknown")
    execution_count = result.get("execution_count")

    parts = []
    if execution_count is None:
        parts.append(f"[{status}]")
    else:
        parts.append(f"[{status}] Execution count: {execution_count}")
    if warning:
        parts.append(f"Warning: {warning}")

    if status == "timeout":
        parts.append("Execution timed out and the kernel was interrupted.")
    elif status == "error":
        error_name = result.get("error_name") or "Error"
        error_value = result.get("error_value") or ""
        parts.append(f"{error_name}: {error_value}")
        parts.extend(_clean_traceback(result.get("traceback"))[:8])

    output_lines = []
    for output in result.get("outputs") or []:
        xml = format_output_xml(output)
        if xml:
            output_lines.append(xml)
    if output_lines:
        parts.append("<outputs>")
        parts.extend(output_lines)
        parts.append("</outputs>")
    elif status == "ok":
        parts.append("(no output)")

    return "\n".join(parts)


async def _wait_until_not_busy(jupyter_svc, kernel_id: str) -> str:
    for _ in range(10):
        await asyncio.sleep(0.5)
        status = await jupyter_svc.get_kernel_status(kernel_id)
        state = status.get("execution_state", "unknown")
        if state != "busy":
            return state
    status = await jupyter_svc.get_kernel_status(kernel_id)
    return status.get("execution_state", "unknown")


# ── notebook_read ────────────────────────────────────────────────────

async def _notebook_read(
    notebook_path: str,
    project_id: str = "",
    session_id: str = "",
    cell_id: str = "",
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """Read Jupyter notebook cells and return as XML."""
    if offset is not None and offset < 0:
        return f"Error: offset must be >= 0, got {offset}"
    if limit is not None and limit <= 0:
        return f"Error: limit must be > 0, got {limit}"

    try:
        notebook, location = await read_notebook_json(notebook_path, project_id)
    except NotebookToolError as exc:
        return f"Error: {exc}"

    cells = notebook.get("cells", [])
    if not cells:
        return "Notebook is empty (no cells)."

    # Determine which cells to return
    output_offset = 0
    output_limit = _CELL_LIST_OUTPUT_LIMIT
    if cell_id:
        idx = find_cell_index(cells, cell_id)
        if idx == -2:
            return f"Error: Cell ID '{cell_id}' matches multiple cells. Provide more characters for a unique match."
        if idx < 0:
            return f"Error: Cell not found: {cell_id}"
        indices = [idx]
        output_offset = offset if offset is not None else 0
        output_limit = limit if limit is not None else _DEFAULT_OUTPUT_LIMIT
    else:
        cell_offset = offset if offset is not None else 0
        cell_limit = limit if limit is not None else _DEFAULT_CELL_LIMIT
        indices = list(range(cell_offset, min(len(cells), cell_offset + cell_limit)))

    # Check kernel status
    kernel_state = ""
    jupyter_svc = get_jupyter()
    if jupyter_svc and await jupyter_svc.is_running():
        jupyter_path = to_jupyter_path(notebook_path, project_id)
        if jupyter_path:
            session = await jupyter_svc.get_session_for_notebook(jupyter_path, create=False)
            if session:
                kernel_info = session.get("kernel", {}) or {}
                kernel_id = kernel_info.get("id")
                if kernel_id:
                    status = await jupyter_svc.get_kernel_status(kernel_id)
                    exec_state = status.get("execution_state", "")
                    if exec_state in ("idle", "busy", "starting", "dead", "unknown"):
                        kernel_state = exec_state

    # Build XML
    attrs = f'cells="{len(cells)}"'
    if cell_id:
        attrs += (
            f' cell_id="{_html_escape(cell_id, quote=True)}"'
            f' offset="{output_offset}" limit="{output_limit}"'
        )
    else:
        attrs += (
            f' offset="{offset if offset is not None else 0}"'
            f' limit="{limit if limit is not None else _DEFAULT_CELL_LIMIT}"'
        )
    if kernel_state:
        attrs += f' kernel="{kernel_state}"'
    parts = [f'<notebook {attrs}>']
    for idx in indices:
        parts.append(format_cell_xml(
            idx,
            cells[idx],
            output_offset=output_offset,
            output_limit=output_limit,
            truncate_hint=True,
        ))
    parts.append("</notebook>")

    if session_id:
        record_path_read(session_id, location.absolute_path, content="", is_partial=False)
    return "\n".join(parts)


# ── notebook_edit ────────────────────────────────────────────────────

async def _notebook_edit(
    notebook_path: str,
    new_string: str = "",
    project_id: str = "",
    session_id: str = "",
    cell_id: str = "",
    cell_type: str = "",
    edit_mode: str = "replace",
    old_string: str = "",
) -> str:
    """Edit a Jupyter notebook cell."""
    try:
        target_location = normalize_notebook_path(notebook_path, project_id)
    except NotebookToolError as exc:
        return f"Error: {exc}"
    if not verify_path_readable_fresh(session_id, target_location.absolute_path):
        return (
            f"Error: Notebook '{notebook_path}' has not been read yet (or has "
            "changed since the last read). Use notebook_read first, then retry "
            "the edit."
        )

    try:
        notebook, location = await read_notebook_json(notebook_path, project_id)
    except NotebookToolError as exc:
        return f"Error: {exc}"

    cells = notebook.get("cells", [])
    if not isinstance(cells, list):
        return "Error: Notebook cells must be an array."

    if edit_mode == "delete":
        idx = find_cell_index(cells, cell_id)
        if idx == -2:
            return f"Error: Cell ID '{cell_id}' matches multiple cells. Provide more characters for a unique match."
        if idx < 0:
            return f"Error: Cell not found: {cell_id}"
        cells.pop(idx)
        result = f"Deleted cell {cell_id}"

    elif edit_mode == "insert":
        if cell_type not in ("code", "markdown"):
            return "Error: cell_type must be 'code' or 'markdown' when inserting a cell."
        new_cell = {
            "cell_type": cell_type,
            "id": _new_cell_id(cells),
            "metadata": {},
            "source": new_string,
        }
        if cell_type == "code":
            ensure_code_cell_fields(new_cell)

        if cell_id:
            idx = find_cell_index(cells, cell_id)
            if idx == -2:
                return f"Error: Cell ID '{cell_id}' matches multiple cells. Provide more characters for a unique match."
            if idx < 0:
                return f"Error: Cell not found: {cell_id}"
            cells.insert(idx + 1, new_cell)
        else:
            cells.insert(0, new_cell)
        result = f"Inserted new {cell_type} cell, ID: {new_cell['id']}"

    elif edit_mode == "replace":
        if cell_type and cell_type not in ("code", "markdown"):
            return "Error: cell_type must be 'code' or 'markdown'."
        idx = find_cell_index(cells, cell_id)
        if idx == -2:
            return f"Error: Cell ID '{cell_id}' matches multiple cells. Provide more characters for a unique match."
        if idx < 0:
            return f"Error: Cell not found: {cell_id}"

        source = cell_source_text(cells[idx])
        if old_string:
            count = source.count(old_string)
            if count == 0:
                return f"Error: old_string not found in cell {cell_id}."
            if count > 1:
                return f"Error: old_string matches {count} locations in cell {cell_id}. Provide a unique match."
            cells[idx]["source"] = source.replace(old_string, new_string, 1)
        else:
            cells[idx]["source"] = new_string

        _apply_cell_type(cells[idx], cell_type)
        result = f"Updated cell {cell_id}"

    else:
        return "Error: edit_mode must be 'replace', 'insert', or 'delete'."

    ensure_cell_ids(notebook)
    if not await save_notebook_json(location, notebook):
        return f"Error: Failed to save notebook: {notebook_path}"

    record_path_read(session_id, location.absolute_path, content="", is_partial=False)
    return result


# ── notebook_run_cell ────────────────────────────────────────────────

async def _notebook_run_cell(
    notebook_path: str,
    cell_id: str = "",
    project_id: str = "",
    session_id: str = "",
    timeout: float = 60.0,
    interrupt: bool = False,
) -> str:
    """Execute a notebook code cell on its kernel, or interrupt the kernel."""
    try:
        target_location = normalize_notebook_path(notebook_path, project_id)
    except NotebookToolError as exc:
        return f"Error: {exc}"
    if not verify_path_readable_fresh(session_id, target_location.absolute_path):
        return (
            f"Error: Notebook '{notebook_path}' has not been read yet (or has "
            "changed since the last read). Use notebook_read first, then retry "
            "the run."
        )

    try:
        notebook, location = await read_notebook_json(notebook_path, project_id)
    except NotebookToolError as exc:
        return f"Error: {exc}"

    jupyter_svc = get_jupyter()
    if not jupyter_svc or not await jupyter_svc.is_running():
        return "Error: Jupyter server is not running. Open a notebook in the editor to start Jupyter."

    session = await jupyter_svc.get_session_for_notebook(location.jupyter_path, create=True)
    if not session:
        return "Error: Could not start a kernel session for this notebook."

    kernel_info = session.get("kernel", {}) or {}
    kernel_id = kernel_info.get("id")
    if not kernel_id:
        return "Error: Session has no kernel ID."

    if interrupt:
        success = await jupyter_svc.interrupt_kernel(kernel_id)
        if not success:
            return "Error: Failed to send interrupt to kernel."
        state = await _wait_until_not_busy(jupyter_svc, kernel_id)
        return f"Kernel interrupted. Current state: {state}"

    if not cell_id:
        return "Error: cell_id is required when not using interrupt mode."

    cells = notebook.get("cells", [])
    if not isinstance(cells, list):
        return "Error: Notebook cells must be an array."

    idx = find_cell_index(cells, cell_id)
    if idx == -2:
        return f"Error: Cell ID '{cell_id}' matches multiple cells. Provide more characters for a unique match."
    if idx < 0:
        return f"Error: Cell not found: {cell_id}"

    cell = cells[idx]
    actual_type = cell.get("cell_type", "unknown")
    if actual_type != "code":
        return f"Error: Cell {cell_id} is a {actual_type} cell, not a code cell. Only code cells can be executed."

    source = cell_source_text(cell)
    if not source.strip():
        return f"Error: Cell {cell_id} is empty."

    status = await jupyter_svc.get_kernel_status(kernel_id)
    execution_state = status.get("execution_state", "unknown")
    if execution_state == "busy":
        return "Error: Kernel is busy. Set interrupt=true to stop the current execution, then try again."
    if execution_state in ("dead", "unknown"):
        return f"Error: Kernel is not available (state: {execution_state}). Restart the notebook kernel and try again."

    result = await jupyter_svc.execute_code(kernel_id, source, timeout=timeout)

    if result.get("status") == "timeout":
        await jupyter_svc.interrupt_kernel(kernel_id)
        await _wait_until_not_busy(jupyter_svc, kernel_id)

    result["traceback"] = _clean_traceback(result.get("traceback"))
    for output in result.get("outputs") or []:
        if output.get("output_type") == "error":
            output["traceback"] = _clean_traceback(output.get("traceback"))

    try:
        latest_notebook, latest_location = await read_notebook_json(notebook_path, project_id)
    except NotebookToolError:
        latest_notebook, latest_location = notebook, location

    latest_cells = latest_notebook.get("cells", [])
    latest_idx = find_cell_index(latest_cells, cell_id)
    if latest_idx < 0:
        return (
            "Error: Cell executed, but outputs were not saved because the target cell "
            f"could no longer be found.\n{_format_run_result(result)}"
        )

    latest_cell = latest_cells[latest_idx]
    if latest_cell.get("cell_type") != "code":
        return (
            "Error: Cell executed, but outputs were not saved because the target cell "
            "is no longer a code cell.\n"
            f"{_format_run_result(result)}"
        )

    warning = ""
    if cell_source_text(latest_cell) != source:
        warning = "cell source changed while execution was running; outputs were saved to the current cell with the same ID"

    ensure_code_cell_fields(latest_cell)
    latest_cell["outputs"] = result.get("outputs") or []
    latest_cell["execution_count"] = result.get("execution_count")

    ensure_cell_ids(latest_notebook)
    if not await save_notebook_json(latest_location, latest_notebook):
        return f"Error: Cell executed, but saving outputs failed.\n{_format_run_result(result)}"

    record_path_read(session_id, latest_location.absolute_path, content="", is_partial=False)
    return _format_run_result(result, warning=warning)


# ── Register tools ───────────────────────────────────────────────────

tool_registry.register(ToolDefinition(
    name="notebook_read",
    description="Read Jupyter notebook cells and their execution outputs in XML format.",
    prompt=PROMPT_NOTEBOOK_READ,
    input_schema={
        "type": "object",
        "properties": {
            "notebook_path": {
                "type": "string",
                "description": "Absolute or project-relative .ipynb path; must stay inside project",
            },
            "cell_id": {
                "type": "string",
                "description": "Optional: read one cell by the ID shown by notebook_read. Omit to page through cells.",
            },
            "offset": {
                "type": "integer",
                "description": "When cell_id is omitted, 0-indexed starting cell index. When cell_id is provided, 0-indexed output start line.",
                "default": 0,
                "minimum": 0,
            },
            "limit": {
                "type": "integer",
                "description": "When cell_id is omitted, number of cells to return (default 5). When cell_id is provided, number of output lines to return (default 200).",
                "minimum": 1,
            },
        },
        "required": ["notebook_path"],
    },
    call=lambda notebook_path, project_id="", session_id="", cell_id="", offset=None, limit=None: _notebook_read(
        notebook_path, project_id, session_id, cell_id, offset, limit,
    ),
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=True,
))

tool_registry.register(ToolDefinition(
    name="notebook_edit",
    description="Edit Jupyter notebook cells (.ipynb). Supports replace, insert, and delete operations.",
    prompt=PROMPT_NOTEBOOK_EDIT,
    input_schema={
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string", "description": "Absolute or project-relative .ipynb path; must stay inside project"},
            "new_string": {"type": "string", "description": "New source text for replace or insert operations."},
            "cell_id": {"type": "string", "description": "Cell ID shown by notebook_read. For insert, the new cell is inserted after this cell. If empty, it is inserted at the beginning."},
            "cell_type": {"type": "string", "enum": ["code", "markdown"], "description": "Cell type. Required for insert; optional for replace."},
            "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"], "description": "Edit mode", "default": "replace"},
            "old_string": {"type": "string", "description": "Replace mode only: if omitted, new_string replaces the entire cell."},
        },
        "required": ["notebook_path"],
    },
    call=lambda notebook_path, new_string="", project_id="", session_id="", cell_id="", cell_type="", edit_mode="replace", old_string="": _notebook_edit(
        notebook_path, new_string, project_id, session_id, cell_id, cell_type, edit_mode, old_string,
    ),
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=False,
))

tool_registry.register(ToolDefinition(
    name="notebook_run_cell",
    description="Execute a code cell in a Jupyter notebook on its kernel and return the output.",
    prompt=PROMPT_NOTEBOOK_RUN_CELL,
    input_schema={
        "type": "object",
        "properties": {
            "notebook_path": {
                "type": "string",
                "description": "Absolute or project-relative .ipynb path; must stay inside project",
            },
            "cell_id": {
                "type": "string",
                "description": "Cell ID shown by notebook_read. Not needed when interrupt=true.",
            },
            "timeout": {
                "type": "number",
                "description": "Execution timeout in seconds (default 60; increase for long computations)",
                "default": 60,
            },
            "interrupt": {
                "type": "boolean",
                "description": "Set to true to interrupt a busy kernel (use when notebook_read shows kernel=\"busy\").",
                "default": False,
            },
        },
        "required": ["notebook_path"],
    },
    call=lambda notebook_path, cell_id="", project_id="", session_id="", timeout=60.0, interrupt=False: _notebook_run_cell(
        notebook_path, cell_id, project_id, session_id, timeout, interrupt,
    ),
    requires_project_id=True,
    requires_session_id=True,
    is_read_only=False,
))
