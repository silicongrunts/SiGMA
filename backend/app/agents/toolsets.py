"""
Centralized tool permission definitions for SiGMA agent system.

All tool access control is defined here. Runtime enforcement happens in
LLMLoopRunner and agent_service — never scatter if-checks for tool names.
"""

# ── Tool categories ──

# Read-only tools: no file modification, no code execution
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    # File system (read-only)
    "read", "ls", "glob", "grep",
    # Library (read-only)
    "library_search", "library_ls", "library_get",
    # Browser
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_input", "browser_scroll", "browser_vision",
    "browser_back", "browser_console", "browser_cdp", "browser_pages",
    # Vision
    "vision_analyze",
    # Notebook (read-only)
    "notebook_read",
    # Annotation (read-only)
    "annotation_get", "annotation_list",
    # Utility
    "sleep",
})

# Tools that write/modify files or execute code
WRITE_EXEC_TOOLS: frozenset[str] = frozenset({
    "write", "edit", "bash",
    "notebook_edit", "notebook_run_cell",
    "library_new", "library_rm", "library_mkdir", "library_mv", "library_update",
    "annotation_new", "annotation_rm", "annotation_reply",
    "skill_load",
    "draw_image",
})

# Tools that require user interaction (interactive / two-phase).
# NOTE: submit_plan_for_approval is NOT here — it is exclusive to PLAN_TOOLS.
INTERACTIVE_TOOLS: frozenset[str] = frozenset({
    "ask_user_question",
})

# Task management tools
TASK_TOOLS: frozenset[str] = frozenset({
    "task_create", "task_update", "task_list", "task_get", "task_write",
})

# ── Agent toolsets ──

# All tools available to general agent (everything except Agent to prevent recursion
# and Task tools — tasks are managed by the main loop, not sub-agents)
ALL_TOOLS_MINUS_AGENT: frozenset[str] = (
    READ_ONLY_TOOLS | WRITE_EXEC_TOOLS | INTERACTIVE_TOOLS
)

# Plan agent: read-only + user clarification + Agent(explore only) + plan approval
PLAN_TOOLS: frozenset[str] = READ_ONLY_TOOLS | {
    "agent", "ask_user_question", "submit_plan_for_approval",
}

# Explore agent: read-only only
EXPLORE_TOOLS: frozenset[str] = READ_ONLY_TOOLS

# Fork mode keeps the parent tool schema visible for request-shape stability,
# but these tools remain blocked at runtime.
FORK_FORBIDDEN_TOOLS: frozenset[str] = frozenset({"agent"}) | TASK_TOOLS

# Annotation context toolset (unchanged from AnnotationLoop whitelist)
ANNOTATION_TOOLS: frozenset[str] = frozenset({
    # File system (read-only)
    "read", "ls", "glob", "grep",
    # Annotation (read-only)
    "annotation_get", "annotation_list",
    # Library (read-only)
    "library_search", "library_ls", "library_get",
    # Browser — full set, mirrors READ_ONLY_TOOLS
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_input", "browser_scroll", "browser_vision",
    "browser_back", "browser_console", "browser_cdp", "browser_pages",
    # Notebook (read-only)
    "notebook_read",
    # Agent (explore only — enforced at runtime)
    "agent",
    # Utility
    "sleep",
    # Internal (synthetic tool for diff validation)
    "_diff_validate",
})

# ── Agent type restrictions per context ──

# Which agent_types can be spawned from each context
ALLOWED_AGENT_TYPES: dict[str, frozenset[str]] = {
    "main": frozenset({"general", "explore", "plan", ""}),  # "" = fork
    "annotation": frozenset({"explore"}),
    "plan": frozenset({"explore"}),
}
