"""
Prompt for the agent tool — describes the four agent execution modes.
"""

PROMPT = """Launch a subagent for complex or isolated work. The call blocks until the subagent finishes; its final report is the tool result.

Agent types:
- 'general': full-capability worker with every tool except agent, the task tools, and submit_plan_for_approval (tasks and plan approval belong to the main loop). Creates a persistent session; the result starts with a <resume_id> tag — pass it as resume_id to continue that session later.
- 'explore': read-only investigation over project files, Library, and browser/web. Runs on the fast model; single-shot, no persistence.
- 'plan': read-only planner that can spawn explore agents. The call blocks until the user approves a plan; the result is the approved plan (rejection feedback goes back to the planner for revision).
- '' (fork): inherits the current conversation context, with some tools forbidden at runtime (nested agents, parent-state changes). Returns only its final assistant message.

Do not use this tool for simple reads, small edits, or short Q&A you can complete directly.

For general/explore/plan the subagent sees nothing of this conversation: write a detailed, self-contained prompt (background, goal, constraints, expected result, and the evidence/paths/IDs it must return). For fork, state the bounded subtask and the expected handoff."""
