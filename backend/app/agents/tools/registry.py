"""
Tool Registry — centralized lookup for all tools.

Tools register themselves via register() and are looked up by name
when the LLM issues a tool call.
"""

from typing import Dict, List, Optional

from app.agents.tools.base import ToolDefinition


class ToolRegistry:
    """Central registry for all tools available to the LLM."""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._schemas_cache: List[dict] | None = None

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool definition."""
        self._tools[tool.name] = tool
        self._schemas_cache = None  # invalidate cache

    def get(self, name: str) -> Optional[ToolDefinition]:
        """Get a tool by name."""
        return self._tools.get(name)

    def get_many(self, names: List[str]) -> List[ToolDefinition]:
        """Get multiple tools by name."""
        return [t for t in (self._tools.get(n) for n in names) if t is not None]

    def list_all(self) -> List[ToolDefinition]:
        """Get all registered tools."""
        return list(self._tools.values())

    def to_openai_schemas(self) -> List[dict]:
        """Get all tools in OpenAI function-calling format (cached)."""
        if self._schemas_cache is None:
            self._schemas_cache = [t.to_openai_schema() for t in self._tools.values()]
        return self._schemas_cache

    @property
    def names(self) -> List[str]:
        return list(self._tools.keys())


# Global singleton
tool_registry = ToolRegistry()
