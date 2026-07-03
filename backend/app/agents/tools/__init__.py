"""
SiGMA Tools — ToolDefinition registry for QueryLoop.

All tools are registered via ToolDefinition and discovered through tool_registry.
"""

from .base import ToolDefinition
from .registry import tool_registry, ToolRegistry

# Import tool modules to trigger auto-registration
from . import file_tools       # noqa: F401 — read, write, edit, glob, grep, ls
from . import bash              # noqa: F401 — bash
from . import agent_tool        # noqa: F401 — agent
from . import task_tools        # noqa: F401 — task_create, task_update, task_list, task_get, task_write
from . import plan_approval_tool  # noqa: F401 — submit_plan_for_approval
from . import sleep             # noqa: F401 — sleep
from . import notebook_tools   # noqa: F401 — notebook_read, notebook_edit, notebook_run_cell
from . import ask_user_question # noqa: F401 — ask_user_question
from . import browser_tools     # noqa: F401 — browser_navigate, browser_snapshot, ...
from . import vision_tools      # noqa: F401 — vision_analyze
from . import library_tools     # noqa: F401 — library_search, library_ls, ...
from . import annotation_tools  # noqa: F401 — annotation_new, annotation_rm, ...
from . import skill_tools       # noqa: F401 — skill_load
from . import draw_tools        # noqa: F401 — draw_image

__all__ = [
    "ToolDefinition", "tool_registry", "ToolRegistry",
]
