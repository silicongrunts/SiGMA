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
# Upper bound on waiting for the killed subprocess to be reaped. SIGKILL is
# normally reaped near-instantly; this only guards the pathological case of an
# uninterruptible (D-state) child so the agent loop never hangs indefinitely.
KILL_GRACE_SECONDS = 5
# Wall-clock time to let SIGCHLD land and update proc.returncode before
# falling back to proc.wait(). asyncio.sleep(0) is not enough: signal delivery
# needs real elapsed time, not a ready-queue yield.
REAP_POLL_SECONDS = 0.05


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


def _close_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close the subprocess stdout/stderr pipe transports.

    On the normal path ``proc.communicate()`` closes these transports as part
    of draining the pipes. The timeout path skips ``communicate()`` (it would
    block until pipe EOF, re-introducing the command's full runtime), so the
    pipe transports are closed explicitly here to release the file
    descriptors promptly instead of waiting for garbage collection.
    """
    for stream in (proc.stdout, proc.stderr):
        transport = getattr(stream, "_transport", None)
        if transport is not None and not transport.is_closing():
            transport.close()


async def _reap_after_kill(proc: asyncio.subprocess.Process) -> None:
    """Wait for a SIGKILLed subprocess to be reaped, without blocking on the
    command's own runtime.

    A second ``proc.communicate()`` would block until the pipes hit EOF (i.e.
    the command's original runtime), which is the bug this fixes. Instead we
    read ``returncode``: once the event loop has processed SIGCHLD it is set
    promptly. ``asyncio.sleep(0)`` is not enough because SIGCHLD delivery
    needs real wall-clock time, not just a ready-queue yield, so we sleep a
    short bounded interval. If SIGCHLD still has not been processed we fall
    back to a bounded ``proc.wait()``; in the cancelled-communicate case
    ``wait()`` may never resolve even though the process is already dead, so
    the bound is a hard safety limit rather than an expected wait.
    """
    await asyncio.sleep(REAP_POLL_SECONDS)
    if proc.returncode is not None:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "bash subprocess did not exit %ss after SIGKILL", KILL_GRACE_SECONDS,
        )
    except Exception:
        # Best-effort reap: the process is already being killed, so a reap
        # failure must not mask the timeout result.
        logger.warning("bash subprocess reap failed after SIGKILL", exc_info=True)


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
            # Reap without re-entering communicate(): a second communicate()
            # blocks until pipe EOF (the command's full runtime), which is the
            # bug this path fixes.
            await _reap_after_kill(proc)
            _close_pipes(proc)
            return _format_output(
                b"", b"", proc.returncode,
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
