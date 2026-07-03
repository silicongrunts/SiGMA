"""Shared utilities for Jupyter notebook tools."""

from __future__ import annotations

import json as _json
import os
import uuid as _uuid
from dataclasses import dataclass
from html import escape as _html_escape
from pathlib import Path

from app.core.atomic_file import atomic_write_text
from app.core.config import settings
from app.core.utils import is_within
from app.services.jupyter_service import get_jupyter


class NotebookToolError(Exception):
    """Raised for user-facing notebook tool failures."""


@dataclass(frozen=True)
class NotebookLocation:
    absolute_path: Path
    project_relative_path: str
    jupyter_path: str


def normalize_notebook_path(notebook_path: str, project_id: str) -> NotebookLocation:
    """Resolve a notebook path inside the project and build its Jupyter path."""
    if not notebook_path or not notebook_path.endswith(".ipynb"):
        raise NotebookToolError("File must be a Jupyter notebook (.ipynb).")

    try:
        project_path = settings.get_project_path(project_id).resolve()
    except Exception as exc:
        raise NotebookToolError("Project path could not be resolved.") from exc

    if os.path.isabs(notebook_path):
        absolute_path = Path(notebook_path).resolve()
    else:
        absolute_path = (project_path / notebook_path).resolve()

    if not is_within(absolute_path, project_path):
        raise NotebookToolError("Notebook path must stay inside the current project.")

    relative_path = str(absolute_path.relative_to(project_path))
    return NotebookLocation(
        absolute_path=absolute_path,
        project_relative_path=relative_path,
        jupyter_path=f"{project_id}/{relative_path}",
    )


def to_jupyter_path(notebook_path: str, project_id: str) -> str | None:
    """Convert a notebook path to a validated Jupyter-relative path."""
    try:
        return normalize_notebook_path(notebook_path, project_id).jupyter_path
    except NotebookToolError:
        return None


def _display_cell_id(cell: dict, index: int) -> str:
    cid = cell.get("id")
    return str(cid) if cid else str(index)


def ensure_cell_ids(notebook: dict) -> bool:
    """Add stable IDs to cells that do not have one.

    Should be called from write paths (notebook_edit, notebook_run_cell)
    right before persistence so the migrated IDs land on disk exactly once.
    Read paths intentionally do not call this — `read_notebook_json` must stay
    side-effect-free (the in-memory fallback IDs from `_display_cell_id` are
    sufficient for `find_cell_index` to keep working).
    """
    cells = notebook.get("cells", [])
    if not isinstance(cells, list):
        return False

    existing = {
        str(cell.get("id"))
        for cell in cells
        if isinstance(cell, dict) and cell.get("id")
    }
    changed = False
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("id"):
            continue
        cell_id = _uuid.uuid4().hex[:8]
        while cell_id in existing:
            cell_id = _uuid.uuid4().hex[:8]
        cell["id"] = cell_id
        existing.add(cell_id)
        changed = True
    return changed


def find_cell_index(cells: list, cell_id: str) -> int:
    """Find a cell by displayed ID. Returns index, -1 if missing, -2 if ambiguous."""
    wanted = str(cell_id or "").strip()
    if not wanted:
        return -1

    exact_matches = [
        i for i, cell in enumerate(cells)
        if _display_cell_id(cell, i) == wanted
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return -2

    prefix_matches = [
        i for i, cell in enumerate(cells)
        if cell.get("id") and str(cell["id"]).startswith(wanted)
    ]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    if len(prefix_matches) > 1:
        return -2
    return -1


def cell_source_text(cell: dict) -> str:
    """Return a cell source as a single string."""
    source = cell.get("source", "")
    if isinstance(source, list):
        return "".join(str(part) for part in source)
    return str(source)


def ensure_code_cell_fields(cell: dict) -> None:
    """Ensure a code cell has the fields Jupyter expects."""
    cell.setdefault("metadata", {})
    cell.setdefault("outputs", [])
    cell.setdefault("execution_count", None)


def strip_execution_fields(cell: dict) -> None:
    """Remove code-only execution fields from a non-code cell."""
    cell.pop("outputs", None)
    cell.pop("execution_count", None)


async def read_notebook_json(
    notebook_path: str,
    project_id: str,
) -> tuple[dict, NotebookLocation]:
    """Read notebook JSON, preferring Jupyter Contents API over filesystem."""
    location = normalize_notebook_path(notebook_path, project_id)
    jupyter_svc = get_jupyter()
    if jupyter_svc:
        notebook = await jupyter_svc.get_notebook(location.jupyter_path)
        if notebook is not None:
            return notebook, location

    try:
        content_raw = location.absolute_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise NotebookToolError(f"Notebook not found: {notebook_path}") from exc
    except UnicodeDecodeError as exc:
        raise NotebookToolError(f"Notebook is not valid UTF-8: {notebook_path}") from exc
    except OSError as exc:
        raise NotebookToolError(f"Could not read notebook: {exc}") from exc

    try:
        notebook = _json.loads(content_raw)
    except _json.JSONDecodeError as exc:
        raise NotebookToolError("Notebook is not valid JSON.") from exc
    if not isinstance(notebook, dict):
        raise NotebookToolError("Notebook JSON must be an object.")
    return notebook, location


async def save_notebook_json(location: NotebookLocation, notebook: dict) -> bool:
    """Persist a notebook through Jupyter when possible, otherwise atomically."""
    jupyter_svc = get_jupyter()
    if jupyter_svc:
        saved = await jupyter_svc.save_notebook(location.jupyter_path, notebook)
        if saved:
            return True

    try:
        location.absolute_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            location.absolute_path,
            _json.dumps(notebook, indent=1, ensure_ascii=False),
        )
        return True
    except OSError:
        return False


def _xml_escape(text: str) -> str:
    """Escape for XML attribute values (double-quote-safe)."""
    return _html_escape(str(text), quote=True)


def _xml_text_escape(text: str) -> str:
    """Escape for XML text content. Quotes are preserved so LLM-read source
    stays readable — `&quot;`/`&#x27;` in `<source>` is noise that obscures
    Python strings, JSON, shell snippets, etc.
    """
    return _html_escape(str(text), quote=False)


def format_output_xml(output: dict) -> str:
    """Format a single cell output as compact XML for the LLM."""
    output_type = output.get("output_type", "")

    if output_type == "stream":
        text = output.get("text", "")
        if isinstance(text, list):
            text = "".join(text)
        name = output.get("name", "stdout")
        return f'<output type="stream" name="{_xml_escape(name)}">{_xml_text_escape(text)}</output>'

    if output_type in ("execute_result", "display_data"):
        data = output.get("data", {}) or {}
        if "text/plain" in data:
            text = data["text/plain"]
            if isinstance(text, list):
                text = "".join(text)
            return f'<output type="{output_type}">{_xml_text_escape(text)}</output>'
        if "image/png" in data:
            return f'<output type="{output_type}">[Image: PNG]</output>'
        if "image/jpeg" in data:
            return f'<output type="{output_type}">[Image: JPEG]</output>'
        fmts = ", ".join(data.keys()) or "unknown"
        return f'<output type="{output_type}">[{_xml_text_escape(fmts)}]</output>'

    if output_type == "error":
        ename = output.get("ename", "Error")
        evalue = output.get("evalue", "")
        traceback = output.get("traceback", [])
        text = "\n".join(str(line) for line in traceback[:8])
        return (
            f'<output type="error" name="{_xml_escape(ename)}">'
            f'{_xml_text_escape(evalue)}\n{_xml_text_escape(text)}</output>'
        )

    return ""


def format_cell_xml(
    index: int,
    cell: dict,
    *,
    output_offset: int = 0,
    output_limit: int | None = None,
    truncate_hint: bool = False,
) -> str:
    """Format a single notebook cell as XML for tool output."""
    cell_type = cell.get("cell_type", "unknown")
    cell_id = _display_cell_id(cell, index)
    source = cell_source_text(cell)

    parts = [
        (
            f'<cell id="{_xml_escape(cell_id)}" '
            f'type="{_xml_escape(cell_type)}" index="{index}">'
        )
    ]
    parts.append(f"  <source>{_xml_text_escape(source)}</source>")

    outputs = cell.get("outputs", [])
    if outputs:
        output_lines = []
        for output in outputs:
            xml = format_output_xml(output)
            if xml:
                output_lines.extend(xml.splitlines())

        start = max(0, output_offset)
        end = len(output_lines) if output_limit is None else min(
            len(output_lines), start + output_limit,
        )
        shown_lines = output_lines[start:end]
        omitted = max(0, len(output_lines) - end)

        parts.append("  <outputs>")
        if start == 0 and end == len(output_lines):
            for line in shown_lines:
                parts.append(f"    {line}")
        else:
            parts.append(
                f'    <output_excerpt offset="{start}" lines="{len(shown_lines)}">'
            )
            parts.extend(f"      {_xml_text_escape(line)}" for line in shown_lines)
            parts.append("    </output_excerpt>")
        if omitted and truncate_hint:
            next_offset = end
            parts.append(
                (
                    f'    <output_truncated omitted_lines="{omitted}">'
                    "Output truncated. Use notebook_read with "
                    f'cell_id="{_xml_escape(cell_id)}" offset="{next_offset}" '
                    f'limit="{output_limit or 200}" to view remaining output lines.'
                    "</output_truncated>"
                )
            )
        parts.append("  </outputs>")

    parts.append("</cell>")
    return "\n".join(parts)
