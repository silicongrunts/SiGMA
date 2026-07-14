"""
Bash permission checker — wires together security validation,
read-only checking, and wrapper/env-var stripping.

Decision flow:
  1. Strip safe wrappers and env vars
  2. Check for command injection (bash_security)
  3. Split compound commands
  4. For each subcommand:
     a. Strip wrappers/env vars again
     b. Check if read-only (bash_readonly)
     c. Check redirect paths (write permission via file_service)
     d. If not read-only → needs approval
  5. If all subcommands are read-only → auto-approve
  6. Otherwise → needs user approval
"""

from app.agents.tools.bash_security import (
    bash_command_is_safe,
    split_compound_command,
    extract_redirect_paths,
    strip_safe_wrappers,
    is_subshell,
)
from app.agents.tools.bash_readonly import is_command_read_only


class BashPermissionResult:
    """Result of bash permission check."""

    def __init__(
        self,
        approved: bool,
        reason: str = "",
        *,
        tool: str = "bash",
        path: str = "",
        operation: str = "",
        content: str = "",
    ):
        self.approved = approved
        self.reason = reason
        self.tool = tool
        self.path = path
        self.operation = operation
        self.content = content

    def __bool__(self) -> bool:
        return self.approved


def check_bash_permission(
    command: str,
) -> BashPermissionResult:
    """Check whether *command* may run without user approval.

    Pure-synchronous: classifies the command and returns a result. Does NOT
    prompt the user — that's the caller's job (see ``permission_executor``).
    """
    if not command or not command.strip():
        return BashPermissionResult(approved=True, reason="Empty command")

    # Step 1: Security checks on the full command
    if not bash_command_is_safe(command):
        return _needs_approval(command, "Command contains injection patterns or dangerous constructs")

    # Step 2: Check for subshell usage
    if is_subshell(command):
        return _needs_approval(command, "Command uses subshell syntax ($() or backticks)")

    # Step 3: Strip safe wrappers for analysis
    stripped = strip_safe_wrappers(command)

    # Step 4: Split compound commands
    subcommands = split_compound_command(stripped)

    if not subcommands:
        return BashPermissionResult(approved=True, reason="Empty after stripping")

    # Step 5: Check each subcommand
    for subcmd in subcommands:
        subcmd_stripped = strip_safe_wrappers(subcmd)

        # Check if read-only
        if is_command_read_only(subcmd_stripped):
            continue  # Safe, check next subcommand

        # Not read-only — needs approval
        return _needs_approval(
            subcmd_stripped,
            f"Command is not in the read-only allowlist: {_base_command(subcmd_stripped)}",
        )

    # Step 6: Check redirect paths for write targets. Report ALL targets so the
    # user sees the full scope when deciding; ``path`` keeps the first one for
    # the permission UI's path-based rules.
    redirect_paths = extract_redirect_paths(command)
    if redirect_paths:
        return _needs_approval(
            command,
            f"Command redirects output to: {', '.join(redirect_paths)}",
            path=redirect_paths[0],
            operation="write",
        )

    # All checks passed — command is read-only
    return BashPermissionResult(approved=True, reason="Command is read-only")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _needs_approval(
    command: str,
    reason: str,
    *,
    tool: str = "bash",
    path: str = "",
    operation: str = "execute",
    content: str = "",
) -> BashPermissionResult:
    """Create a result indicating the command needs user approval."""
    if not content:
        content = command[:800]
    if not path:
        path = _base_command(command)
    return BashPermissionResult(
        approved=False,
        reason=reason,
        tool=tool,
        path=path,
        operation=operation,
        content=content,
    )


def _base_command(command: str) -> str:
    """Extract the base command (first word) from a command string."""
    stripped = strip_safe_wrappers(command).strip()
    tokens = stripped.split(None, 1)
    return tokens[0] if tokens else stripped
