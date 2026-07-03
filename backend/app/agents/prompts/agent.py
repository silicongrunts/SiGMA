"""
Prompt for the agent tool — describes the five agent execution modes.
"""

PROMPT = """Launch a specialized subagent to handle complex tasks autonomously.

The agent tool launches specialized agents that autonomously handle complex tasks.
Each agent type has specific capabilities and tools available to it.

## Agent Types

- **general** (agent_type="general"): Full-capability agent with all tools except agent.
  Creates a persistent session that can be resumed later with resume_id.
  Use for: implementation work, multi-step analysis, code refactoring.

- **explore** (agent_type="explore"): Fast read-only agent for codebase exploration.
  Uses the fast model. No file modifications allowed.
  Use for: finding files, searching patterns, understanding architecture.

- **plan** (agent_type="plan"): Read-only architect agent that creates implementation plans.
  Can spawn explore agents. Plans require user approval before saving.
  Use for: designing implementation strategy for complex tasks.

- **fork** (agent_type="" or empty): Inherits the current conversation context.
  No persistence. Runtime guardrails may reject tools that would nest agents or
  modify parent task state.
  Use for: context-dependent subtasks whose intermediate work should not remain
  in the parent context.

## Resume

General agents create persistent sessions. To continue a previous general agent
session, pass its resume_id:
  agent(prompt="Continue the analysis", resume_id="<session_id>")

## When NOT to use the agent tool:
- If you want to read a specific file path, use read or glob instead
- If you are searching for a specific class definition, use glob instead
- If you are searching code within a specific file, use read instead

## Usage notes:
- Each agent invocation is synchronous — it blocks until the subagent completes
- Provide a complete task description in the prompt parameter
- Clearly state whether the agent should write code or just research
- For fork mode, clearly state the bounded subtask and what the final handoff
  must include
"""
