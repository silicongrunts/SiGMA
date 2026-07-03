"""
bash tool — execute shell commands in the project directory.

This is NOT a file operation tool — it lives in its own file
per the project architecture rule.
"""

import asyncio

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.prompts import PROMPT_BASH
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_TIMEOUT_SECONDS = 600


def _format_output(stdout: bytes, stderr: bytes, exit_code, *, note: str = "") -> str:
    """Format command output as three labeled sections. ``note`` adds a
    parenthetical annotation to the exit-code line (used for timeout)."""
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
    code_line = f"exit code: {exit_code}"
    if note:
        code_line += f"  ({note})"
    return (
        f"stdout: {stdout_text}\n"
        f"-----\n"
        f"stderr: {stderr_text}\n"
        f"-----\n"
        f"{code_line}"
    )


async def _run_bash(project_id: str, command: str, timeout: int = 120) -> str:
    """Execute a bash command in the project directory."""
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        return f"Error: timeout {timeout!r}s is invalid (must be integer in [1, {MAX_TIMEOUT_SECONDS}])"

    project_path = settings.get_project_path(project_id)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                stdout, stderr = await proc.communicate()
            except Exception:
                stdout, stderr = b"", b""
            return _format_output(
                stdout, stderr, proc.returncode,
                note=f"Command timed out after {timeout}s",
            )

        return _format_output(stdout, stderr, proc.returncode)
    except Exception as e:
        logger.exception("bash tool failed")
        return f"Bash error: {e}"


# ── Register ──

tool_registry.register(ToolDefinition(
    name="bash",
    description="Executes a given bash command and returns its output.",
    prompt=PROMPT_BASH,
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute"},
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds (1..{MAX_TIMEOUT_SECONDS})",
                "default": 120, "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS,
            },
            "description": {
                "type": "string",
                "description": "Clear, concise description of what this command does",
                "default": "", "maxLength": 200,
            },
        },
        "required": ["command"],
    },
    call=lambda command, project_id, timeout=120, description="": _run_bash(project_id, command, timeout),
    requires_project_id=True,
    is_read_only=False,
))
