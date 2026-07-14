"""
Agent Tool — the main LLM delegates to subagents via this tool.

Schema: Agent(agent_type, prompt, resume_id)
- agent_type: "general" | "explore" | "plan" | "" (fork)
- prompt: instruction text (required)
- resume_id: existing general agent session ID to resume (optional)

Execution context (messages, tool_call_id, etc.) is passed via the
``agent_exec_context`` contextvar, which is set by the loop runner before
the agent tool runs.  This ContextVar is a deliberately-public cross-module
contract between ``services.llm_loop_runner`` and this module; the absence
of a leading underscore reflects that.
"""

import contextvars

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry

# Cross-module execution context.  The loop runner populates this right
# before dispatching the agent tool; the tool reads it to recover the
# parent's messages, permission requester, and event hooks.
agent_exec_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "agent_exec_context", default=None
)


async def _run_agent(
    agent_type: str = "",
    prompt: str = "",
    resume_id: str = "",
    project_id: str = "",
    session_id: str = "",
    **_kwargs,
) -> str:
    """Execute a subagent and return the result string."""
    ctx = agent_exec_context.get()
    if ctx is None:
        return "Error: Agent execution context not available. Cannot run agent."

    from app.services.agent_service import agent_service

    context_kind = ctx.get("context_kind", "main")

    return await agent_service.run_agent(
        agent_type=agent_type,
        prompt=prompt,
        resume_id=resume_id or None,
        project_id=project_id,
        parent_session_id=session_id,
        parent_tool_call_id=ctx.get("tool_call_id", ""),
        context_kind=context_kind,
        inherited_messages=ctx.get("messages"),
        emit_event=ctx.get("emit_event"),
        cancel_event=ctx.get("cancel_event"),
        token_budget_tracker=ctx.get("token_budget_tracker"),
        parent_model_role=ctx.get("model_role"),
        parent_response_max_tokens=ctx.get("response_max_tokens"),
        parent_tool_schemas=ctx.get("tool_schemas"),
    )


async def _run_agent_validated(**kwargs) -> str:
    """Validate parameters before dispatching to agent_service."""
    agent_type = kwargs.get("agent_type", "")
    prompt = kwargs.get("prompt", "")
    resume_id = kwargs.get("resume_id", "")

    if not prompt or not prompt.strip():
        return "Error: prompt is required for the agent tool."

    valid_types = {"general", "explore", "plan", ""}
    if agent_type not in valid_types:
        names = ", ".join(sorted(t for t in valid_types if t))
        return (
            f"Error: Unknown agent type '{agent_type}'. "
            f"Available types: {names}, or empty for fork mode."
        )

    return await _run_agent(**kwargs)


tool_registry.register(ToolDefinition(
    name="agent",
    description="Launch a specialized subagent to handle a task",
    input_schema={
        "type": "object",
        "properties": {
            "agent_type": {
                "type": "string",
                "description": (
                    "'general' for full-capability execution of decomposed complex tasks; "
                    "'explore' for read-only project / library / browser investigation; "
                    "'plan' for read-only planning before complex tasks, including user clarification and plan approval; "
                    "'' (empty string) for fork mode, which inherits the full conversation context and is preferred "
                    "for context-dependent tasks whose intermediate context does not need to be retained. "
                ),
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The task instruction for the subagent. IMPORTANT: For 'general', 'explore', and 'plan', "
                    "you MUST provide a detailed self-contained prompt because they cannot see the full conversation history."
                )
            },
            "resume_id": {
                "type": "string",
                "description": (
                    "resume_id of an existing general agent session to resume. "
                    "Only general agent sessions can be resumed."
                ),
            },
        },
        "required": ["prompt"],
    },
    call=_run_agent_validated,
    prompt=(
        "Launch a subagent for complex or isolated work.\n\n Agent types:\n"
        "- 'general': full-capability worker for decomposed complex tasks; "
        "resumable. (will return <resume_id> tag in result)\n"
        "- 'explore': read-only investigation over project files, Library, and browser/web."
        "Fast model, no file modifications.\n"
        "- 'plan': Read-only agent that can explore and create plans. "
        "(will return a user approved plan)\n"
        "- '' (fork): Inherits the current conversation context and returns "
        "only its final assistant message as the tool result. \n\n"
        "Do not use for simple reads, small edits, short Q&A, "
        "or tasks you can complete directly with low context cost \n\n"
        "For general / explore / plan, write a detailed self-contained prompt. "
        "For fork, write a bounded subtask prompt with the expected handoff details. "
    ),
    is_agent_tool=True,
    requires_project_id=True,
    requires_session_id=True,
))
