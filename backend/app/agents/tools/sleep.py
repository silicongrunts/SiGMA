"""
sleep tool — wait for a specified duration.
"""

import asyncio

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.prompts import PROMPT_SLEEP


_MAX_DURATION = 300


async def _sleep(duration: int = 5) -> str:
    """Sleep for the specified duration in seconds.

    Negative values are rejected. Values above ``_MAX_DURATION`` are silently
    capped (with a note in the return string so the LLM knows).
    """
    if duration < 0:
        return f"Error: duration must be non-negative, got {duration}"
    actual = min(duration, _MAX_DURATION)
    await asyncio.sleep(actual)
    if actual < duration:
        return (
            f"Slept for {actual} seconds "
            f"(requested {duration}, capped at {_MAX_DURATION})."
        )
    return f"Slept for {actual} seconds."


tool_registry.register(ToolDefinition(
    name="sleep",
    description="Wait for a specified duration.",
    prompt=PROMPT_SLEEP,
    input_schema={
        "type": "object",
        "properties": {
            "duration": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 5,
                "minimum": 0,
            },
        },
        "required": [],
    },
    call=lambda duration=5: _sleep(duration),
    is_read_only=True,
))
