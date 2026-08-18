"""
Tool definition base — each tool is a self-contained unit.

Each tool is a ToolDefinition with name, input_schema, prompt,
and an async call function. The schema uses JSON Schema format so it can
be passed directly to LLM APIs.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional


@dataclass
class ToolDefinition:
    """Definition of a tool the LLM can call.

    Each tool has a name and a prompt (the detailed description sent to the
    LLM as the tool's schema description).
    Tools that need user interaction set requires_user_interaction=True;
    the QueryLoop will pause and wait for a response.
    """

    name: str
    input_schema: dict = field(default_factory=dict)
    call: Callable[..., Awaitable[Any]] | None = None
    requires_project_id: bool = False
    requires_session_id: bool = False
    requires_model_role: bool = False
    # ── Extended tool attributes ──
    prompt: str = ""                      # The tool's schema description sent to the LLM
    requires_user_interaction: bool = False  # Pauses loop for user input
    is_read_only: bool = False            # Tool does not modify files
    is_agent_tool: bool = False           # Tool runs an LLM sub-loop (Agent)
    # Optional deterministic pre-check run by the permission executor *before*
    # the approval dialog. Returns the first error string (fed back to the LLM,
    # same path as a denied tool — no dialog is shown) or ``None`` to proceed.
    # Use it for checks that are guaranteed to fail at execution time but do not
    # depend on side effects: e.g. the must-read-first contract, edit's
    # old==new invariant, notebook cell lookups. Receives the same kwargs the
    # permission executor sees (project_id/session_id injected by the runner).
    preflight: Callable[..., Awaitable[Optional[str]]] | None = None

    def to_openai_schema(self) -> dict:
        """Convert to OpenAI function-calling format.

        The prompt is the function description, so behavioral constraints
        travel in the structured tools parameter rather than the system
        prompt text.
        """
        schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.prompt,
            },
        }
        if self.input_schema:
            schema["function"]["parameters"] = self.input_schema
        return schema

