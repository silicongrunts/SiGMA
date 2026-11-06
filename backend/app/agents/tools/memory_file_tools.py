"""Memory-scoped file removal tool (``rm``).

Audience: the background memory consolidation agent only. The tool is
registered with ``internal=True``: invisible in every advertised catalog
(see ``ToolDefinition.internal``) and refused by the generic dispatch paths
(permission gate, default tool execution). The one legitimate caller is
``memory_agent_loop``, which allow-lists it for consolidation runs and
enforces the memory-root containment below — a hallucinated ``rm`` in any
foreground, subagent, or annotation loop is a denial, never a delete.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.core.logging import get_logger
from app.services.memory_service import (
    MEMORY_INDEX_NAME,
    memory_root,
    resolve_memory_file,
)

logger = get_logger(__name__)

_PROMPT_RM = (
    "Delete a file inside the project memory directory (.SiGMA/memory). "
    "Use it to remove orphaned or superseded memory fact files. Refuses to "
    "delete MEMORY.md (fix the index lines instead) and refuses any path "
    "outside the memory directory."
)


async def _rm_memory_file(project_id: str, session_id: str, path: str) -> str:
    del session_id  # uniform tool signature; no read-state involved
    resolved = resolve_memory_file(project_id, path)
    if resolved is None:
        return f"Error: '{path}' is outside the project memory directory."

    if resolved.name == MEMORY_INDEX_NAME:
        return "Error: MEMORY.md is the memory index — it cannot be deleted. Edit its lines instead."

    def _unlink() -> None:
        resolved.unlink()

    try:
        if not resolved.is_file():
            return f"Error: File not found: {path}"
        await asyncio.to_thread(_unlink)
    except OSError as exc:
        logger.warning("Memory rm failed for %s/%s: %s", project_id, path, exc)
        return f"Error: Could not delete '{path}': {exc}"
    logger.info("Memory file removed for %s: %s", project_id, resolved)
    return f"Deleted memory file: {resolved.name}"


tool_registry.register(ToolDefinition(
    name="rm",
    prompt=_PROMPT_RM,
    internal=True,
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The memory file to delete (memory-directory path)",
            },
        },
        "required": ["path"],
    },
    call=lambda path, project_id, session_id: _rm_memory_file(
        project_id, session_id, path,
    ),
))
