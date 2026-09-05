"""
bash tool — execute shell commands in the project directory.

This is NOT a file operation tool — it lives in its own file
per the project architecture rule.
"""

import asyncio
import os
import signal
import shlex
import sys
from pathlib import Path
from typing import Optional

from app.agents.tools.base import ToolDefinition
from app.agents.tools.registry import tool_registry
from app.agents.prompts import PROMPT_BASH
from app.core.config import settings
from app.core.async_cleanup import finish_cleanup
from app.core.logging import get_logger

logger = get_logger(__name__)

MAX_TIMEOUT_SECONDS = 3600
DEFAULT_TIMEOUT_SECONDS = 60
# Wall-clock grace between the SIGINT sent to a timed-out command's group and
# the targeted SIGKILL sweep over whatever is still running.
SIGINT_GRACE_SECONDS = 3
# Upper bound on waiting for the killed subprocess to be reaped. SIGKILL is
# normally reaped near-instantly; this only guards the pathological case of an
# uninterruptible (D-state) child so the agent loop never hangs indefinitely.
KILL_GRACE_SECONDS = 5
# SigIgn bitmap bit for SIGINT in /proc/<pid>/status (bit = signal number - 1).
_SIGINT_MASK = 1 << (signal.SIGINT - 1)
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


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the command's whole process group.

    ``create_subprocess_shell`` runs ``/bin/sh -c <cmd>``; killing only the
    shell orphans its children (``sh -c 'sleep 300; echo x'`` leaves the
    sleep running past both timeout and cancellation). The shell is spawned
    with ``start_new_session=True`` so its group id equals its pid and the
    kill cannot touch SiGMA's own workers. Blunt by design — every group
    member dies, backgrounded jobs included; _terminate_foreground uses it
    only as the non-Linux fallback.

    The pid guard is load-bearing: ``os.killpg(pgid, sig)`` translates to
    ``kill(-pgid, sig)``, so a pid of exactly 1 (or any non-int) becomes
    ``kill(-1)`` — SIGKILL to every process on the system when the backend
    runs as root. A real child's pid is always an int ≥ 2; anything else
    must be refused outright rather than coerced.
    """
    pid = proc.pid
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        logger.warning("refusing to kill process group for bogus pid %r", pid)
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        # Group already gone (or not a leader) — fall back to a direct kill.
        if proc.returncode is None:
            try:
                proc.kill()
            except OSError:
                pass


def _sigint_ignored(pid: int) -> bool:
    """True if *pid* ignores SIGINT (SigIgn bit set in /proc/<pid>/status)."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as status_file:
            for line in status_file:
                if line.startswith("SigIgn:"):
                    return bool(int(line.split()[1], 16) & _SIGINT_MASK)
    except (OSError, ValueError):
        return False
    return False


def _foreground_group_members(pgid: int) -> Optional[list[int]]:
    """PIDs in *pgid* that do NOT ignore SIGINT — i.e. the foreground tree.

    POSIX shells start asynchronous (``&``) jobs with SIGINT and SIGQUIT set
    to SIG_IGN, so the group members that ignore SIGINT are exactly the jobs
    the command deliberately backgrounded. Returns None when /proc is
    unavailable (non-Linux) so callers can fall back to a blunter policy.
    """
    if not os.path.isdir("/proc"):
        return None
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if os.getpgid(pid) != pgid:
                continue
        except OSError:
            continue
        if not _sigint_ignored(pid):
            members.append(pid)
    return members


def _kill_foreground_members(pgid: int) -> Optional[bool]:
    """SIGKILL the SIGINT-resistant members of *pgid* left after the grace
    window; deliberately backgrounded jobs (SIG_IGN) are spared.

    Returns True when any process was signalled, False when the foreground
    tree is already gone, None when /proc is unavailable (non-Linux).
    """
    members = _foreground_group_members(pgid)
    if members is None:
        return None
    killed = False
    for pid in members:
        try:
            os.kill(pid, signal.SIGKILL)
            killed = True
        except OSError:
            continue
    return killed


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


async def _reap_after_signal(
    proc: asyncio.subprocess.Process, wait_seconds: float,
) -> None:
    """Wait bounded for a signalled subprocess to be reaped, without blocking
    on the command's own runtime.

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
        await asyncio.wait_for(proc.wait(), timeout=wait_seconds)
    except asyncio.TimeoutError:
        logger.warning(
            "bash subprocess did not exit %ss after signal", wait_seconds,
        )
    except Exception:
        # Best-effort reap: the process is already being killed, so a reap
        # failure must not mask the timeout result.
        logger.warning("bash subprocess reap failed after signal", exc_info=True)


async def _terminate_foreground(proc: asyncio.subprocess.Process) -> None:
    """Stop a timed-out or cancelled command's foreground tree; leave jobs it
    deliberately backgrounded (``nohup ... &``, ``python server.py &``) alive.

    The command runs in its own session (``start_new_session=True``), so
    ``os.killpg`` cannot reach SiGMA's own workers. A group-wide SIGINT kills
    the foreground pipeline while POSIX SIG_IGN spares background jobs. After
    ``SIGINT_GRACE_SECONDS`` the sweep SIGKILLs only the group members that
    survived SIGINT (they installed their own handler); backgrounded jobs are
    spared because their SIGINT is still SIG_IGN. On non-Linux (no /proc) we
    fall back to killing the whole group, but only while the shell itself is
    still alive.
    """
    pid = proc.pid
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        logger.warning("refusing to signal process group for bogus pid %r", pid)
        return
    try:
        os.killpg(pid, signal.SIGINT)
    except OSError:
        return  # group already gone — nothing left to stop
    await _reap_after_signal(proc, SIGINT_GRACE_SECONDS)
    killed = _kill_foreground_members(pid)
    if killed is None:
        # No /proc: cannot tell foreground from background, so restrict the
        # blunt group kill to the case where the shell is demonstrably alive.
        if proc.returncode is None:
            _kill_process_group(proc)
            await _reap_after_signal(proc, KILL_GRACE_SECONDS)
    elif killed:
        await _reap_after_signal(proc, KILL_GRACE_SECONDS)


async def _run_bash(
    project_id: str, command: str, timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Execute a bash command in the project directory."""
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        return f"Error: timeout {timeout!r}s is invalid (must be integer in [1, {MAX_TIMEOUT_SECONDS}])"
    requested_timeout = timeout
    if timeout > MAX_TIMEOUT_SECONDS:
        # Clamp like the sleep tool; the cap only shows up in the result if
        # the command actually runs into it.
        timeout = MAX_TIMEOUT_SECONDS

    project_path = settings.get_project_path(project_id)
    worker = Path(__file__).resolve().parents[2] / "core" / "bash_worker.py"
    invocation = shlex.join([sys.executable, str(worker), str(os.getpid()), command])
    try:
        proc = await asyncio.create_subprocess_shell(
            "exec " + invocation,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
            # Own process group so a timeout/cancel can stop the command's
            # foreground tree without touching SiGMA's own workers (see
            # _terminate_foreground).
            start_new_session=True,
        )
        note = ""
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            stdout, stderr = b"", b""
            note = f"Command timed out after {timeout}s"
            if timeout < requested_timeout:
                note += (
                    f" (requested {requested_timeout}s, "
                    f"capped at {MAX_TIMEOUT_SECONDS}s)"
                )
            await finish_cleanup(_terminate_foreground(proc))
        except BaseException:
            # Cancelled (or the pipes broke) mid-run: stop the foreground
            # tree with the same policy as timeout before propagating.
            await finish_cleanup(_terminate_foreground(proc))
            raise
        finally:
            _close_pipes(proc)

        return _format_output(stdout, stderr, proc.returncode, note=note)
    except Exception as e:
        logger.exception("bash tool failed")
        return f"Bash error: {e}"


# ── Register ──

tool_registry.register(ToolDefinition(
    name="bash",
    prompt=PROMPT_BASH,
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute"},
            "timeout": {
                "type": "integer",
                "description": (
                    f"Timeout in seconds (1..{MAX_TIMEOUT_SECONDS}; "
                    f"values above {MAX_TIMEOUT_SECONDS} are capped)"
                ),
                "default": DEFAULT_TIMEOUT_SECONDS, "minimum": 1,
            },
            "description": {
                "type": "string",
                "description": "Clear, concise description of what this command does",
                "default": "", "maxLength": 200,
            },
        },
        "required": ["command"],
    },
    call=lambda command, project_id, timeout=DEFAULT_TIMEOUT_SECONDS, description="": _run_bash(project_id, command, timeout),
    requires_project_id=True,
    is_read_only=False,
))
