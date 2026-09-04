"""Pause protocol exceptions shared by the LLM loop, the permission
executor, and agent orchestration.

These classes live in a dependency-free module because the import graph
forces it: ``permission_executor`` imports ``LLMLoopRunner`` (to invoke
tools after approval) while ``llm_loop_runner`` must recognize permission
pauses — a leaf module is the only way both can share the classes without
a circular import.
"""

from app.core.text_diff import CONTENT_SOFT_LIMIT


class PermissionRequestPause(Exception):
    """Raised when a tool needs user approval before it can execute.

    Carries the full context needed to render the approval dialog and to resume
    the task after the user responds. The LLM loop runner catches this, persists
    a checkpoint via ``mark_awaiting_input``, and emits an ``awaiting_input``
    SSE event. The user's response flows back through the resume path
    (``QueryLoop._resume_from_permission``).
    """

    def __init__(
        self,
        *,
        tool: str,
        tool_name: str = "",
        path: str = "",
        operation: str = "",
        content: str = "",
        description: str = "",
        diff_lines: list | None = None,
        diff_truncated: bool = False,
        resolved_path: str = "",
        content_sha256: str = "",
    ):
        self.tool = tool              # category: file_external/file_internal/bash/notebook
        self.tool_name = tool_name    # concrete tool invoked: write/edit/bash/...
        self.path = path
        self.operation = operation
        # The flat preview content shown in the approval dialog, capped at
        # CONTENT_SOFT_LIMIT like diff_lines: the pause frame is buffered
        # per subscriber and persisted in the interaction checkpoint, so one
        # huge write approval must not park a multi-megabyte frame there.
        if len(content) > CONTENT_SOFT_LIMIT:
            content = content[:CONTENT_SOFT_LIMIT]
            self.content_truncated = True
        else:
            self.content_truncated = False
        self.content = content
        self.description = description
        # Snapshot of the write target resolved at approval time. The resume
        # path re-resolves the target and refuses to execute when it no
        # longer matches, so a symlink flipped while the task was parked
        # cannot redirect an approved write. Empty for categories without a
        # filesystem target (bash / notebook); empty for a file category
        # means the target could not be resolved at approval time, and the
        # resume path refuses execution for that pause.
        self.resolved_path = resolved_path
        # sha256 of the full approval content, hashed before the CONTENT_SOFT_LIMIT
        # truncation above. Categories whose approved operation must match what the
        # user reviewed (notebook cell execution) set it; the resume path re-hashes
        # the current content and refuses to execute on a mismatch. Empty means no
        # replay verification — including checkpoints saved before the field existed.
        self.content_sha256 = content_sha256
        # Structured diff for the ``edit`` tool (old_string -> new_string);
        # ``None`` for other tools, which fall back to the flat ``content``.
        # ``diff_truncated`` is set when the diff exceeded DIFF_LINE_SOFT_LIMIT.
        self.diff_lines = diff_lines
        self.diff_truncated = diff_truncated
        # Set by the runner when the pause propagates out of a subagent, so
        # the parent loop knows which agent tool_call to attach the result to.
        self.parent_tool_call_id = ""
        # Enriched by agent_service when the pause escapes a subagent, so the
        # checkpoint can be saved as a subagent interaction and the subagent
        # resumed mid-loop after the user responds (same pattern as
        # InteractiveToolPause). Empty for direct (main-loop) tool pauses.
        self.agent_session_id = ""
        self.agent_type = ""
        self.agent_usage_baseline: dict | None = None
        # Subagent spend accrued since the enclosing agent call started,
        # stamped by the loop runner when the pause propagates out of it.
        # Persisted in the interaction checkpoint so the turn token stats
        # keep counting that spend across the pause/resume split (the
        # parent's completion delta row only covers the post-resume diff).
        self.agent_usage_carry: dict | None = None
        self.inner_tool_call_id = ""
        # The full tool_args of the paused tool call. Set by the runner when it
        # catches the pause (the runner has the LLM-produced args). Needed to
        # re-execute the tool on resume after user approval.
        self.tool_args: dict = {}
        super().__init__(f"Permission required for {tool_name or tool}: {operation} {path}")


class InteractiveToolPause(Exception):
    """Raised when a subagent encounters an interactive tool and cannot pause.

    Propagates to the parent loop, which saves its own checkpoint and
    pauses on behalf of the subagent.  Carries all state needed for resume.
    """

    def __init__(
        self,
        *,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
        interaction_data: dict,
        agent_session_id: str = "",
        agent_type: str = "",
        parent_tool_call_id: str = "",
        agent_usage_baseline: dict | None = None,
    ):
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_call_id = tool_call_id
        self.interaction_data = interaction_data
        self.agent_session_id = agent_session_id
        self.agent_type = agent_type
        self.parent_tool_call_id = parent_tool_call_id
        self.agent_usage_baseline = agent_usage_baseline
        # Same contract as PermissionRequestPause.agent_usage_carry: stamped
        # by the loop runner when this pause propagates out of an agent call.
        self.agent_usage_carry: dict | None = None
        super().__init__(
            f"Interactive tool '{tool_name}' requires user input in subagent"
        )


def is_permission_pause(exc: BaseException) -> bool:
    """Return True when *exc* is a permission pause."""
    return isinstance(exc, PermissionRequestPause)
