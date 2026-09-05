"""
Unit tests for the bash tool.

Covers:
- ``_format_output`` produces the unified three-section format in all cases
- ``_run_bash`` validates timeout, caps too-large timeouts, stops the
  foreground tree on timeout (SIGINT, then targeted SIGKILL) while sparing
  deliberately backgrounded jobs, returns the timeout note promptly (without
  blocking for the command's full duration), and uses the unified format on
  success/failure/timeout.
- spawn/execute failures surface as a ``Bash error:`` string instead of
  escaping into the agent loop.
"""

import asyncio
import inspect
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.tools import bash as bash_module
from app.agents.tools.bash import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    _format_output,
    _run_bash,
)


async def _raise_timeout(awaitable, timeout):
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise asyncio.TimeoutError


def _raise_timeout_first_call():
    """A wait_for fake that raises TimeoutError on the first call (the
    communicate() guard) and runs the real awaitable on subsequent calls
    (the post-kill reap), so the reap path can be exercised."""
    call_count = {"n": 0}

    async def _fake(awaitable, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.TimeoutError
        return await awaitable

    return _fake


# ── _format_output ──────────────────────────────────────────────────

def test_format_output_success_with_stdout():
    out = _format_output(b"hello\n", b"", 0)
    assert "stdout: hello" in out
    assert "stderr: " in out
    assert "exit code: 0" in out
    assert out.count("-----") == 2


def test_format_output_failure_with_stderr():
    out = _format_output(b"", b"oops\n", 1)
    assert "stdout: " in out
    assert "stderr: oops" in out
    assert "exit code: 1" in out


def test_format_output_empty_all():
    out = _format_output(b"", b"", 0)
    # Even with no output, all three sections render
    assert "stdout: \n-----" in out
    assert "stderr: \n-----" in out
    assert "exit code: 0" in out


def test_format_output_timeout_note_appended_to_exit_code():
    out = _format_output(b"partial\n", b"", -15,
                        note="Command timed out after 2s")
    assert "exit code: -15  (Command timed out after 2s)" in out
    assert "stdout: partial" in out


# ── _run_bash: timeout validation ───────────────────────────────────

@pytest.mark.asyncio
async def test_run_bash_caps_timeout_above_max():
    """Values above MAX_TIMEOUT_SECONDS are clamped, not rejected."""
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell") as mock_spawn:
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_spawn.return_value = mock_proc
        result = await _run_bash("proj", "true", timeout=MAX_TIMEOUT_SECONDS + 1)
    assert "Error" not in result
    assert "exit code: 0" in result


@pytest.mark.asyncio
async def test_run_bash_rejects_timeout_zero():
    result = await _run_bash("proj", "ls", timeout=0)
    assert "Error: timeout 0s is invalid" in result


@pytest.mark.asyncio
async def test_run_bash_rejects_timeout_negative():
    result = await _run_bash("proj", "ls", timeout=-1)
    assert "Error: timeout -1s is invalid" in result


@pytest.mark.asyncio
async def test_run_bash_rejects_timeout_bool():
    # bool is a subclass of int; we explicitly reject it
    result = await _run_bash("proj", "ls", timeout=True)
    assert "Error: timeout True" in result
    assert "invalid" in result


@pytest.mark.asyncio
async def test_run_bash_rejects_timeout_string():
    result = await _run_bash("proj", "ls", timeout="60")  # type: ignore[arg-type]
    assert "Error: timeout '60'" in result


@pytest.mark.asyncio
async def test_run_bash_accepts_max_timeout():
    """The boundary value should be accepted (no error string, no execution)."""
    # We patch create_subprocess_shell to verify the call goes through
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell") as mock_spawn:
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0
        mock_spawn.return_value = mock_proc
        result = await _run_bash("proj", "true", timeout=MAX_TIMEOUT_SECONDS)
    assert "Error" not in result
    assert "exit code: 0" in result


# ── _run_bash: success / failure ────────────────────────────────────

@pytest.mark.asyncio
async def test_run_bash_success_returns_unified_format():
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell") as mock_spawn:
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
        mock_proc.returncode = 0
        mock_spawn.return_value = mock_proc
        result = await _run_bash("proj", "echo ok")
    assert "stdout: ok" in result
    assert "exit code: 0" in result


@pytest.mark.asyncio
async def test_run_bash_failure_returns_nonzero_exit_code():
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell") as mock_spawn:
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"fail\n"))
        mock_proc.returncode = 2
        mock_spawn.return_value = mock_proc
        result = await _run_bash("proj", "false")
    assert "exit code: 2" in result
    assert "stderr: fail" in result


# ── _run_bash: exceptions must not escape to the loop ───────────────

@pytest.mark.asyncio
async def test_run_bash_spawn_failure_returns_error_string():
    """A spawn/execute exception must surface as a structured
    ``Bash error:`` string for the LLM — it must never propagate into the
    loop runner and abort the conversation turn."""
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell",
               new=AsyncMock(side_effect=OSError("fd exhausted"))):
        result = await _run_bash("proj", "echo hi")

    assert result.startswith("Bash error:")
    assert "fd exhausted" in result


# ── _run_bash: timeout stops the foreground tree ────────────────────

@pytest.mark.asyncio
async def test_run_bash_timeout_interrupts_foreground_then_sweeps():
    """On timeout: the group first gets SIGINT (POSIX background jobs ignore
    it), then after the grace window only the members still running — the
    ones that did not die from SIGINT — are SIGKILLed individually."""
    killpg_targets = []
    direct_kills = []

    async def slow_communicate():
        await asyncio.sleep(10)
        return b"", b""

    mock_proc = MagicMock()
    mock_proc.communicate = slow_communicate
    mock_proc.pid = 4242
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock()

    def killpg_side_effect(pgid, sig):
        killpg_targets.append((pgid, sig))

    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell",
               return_value=mock_proc), \
         patch("app.agents.tools.bash.asyncio.wait_for",
               new=_raise_timeout_first_call()), \
         patch("app.agents.tools.bash.SIGINT_GRACE_SECONDS", 0.05), \
         patch("app.agents.tools.bash._foreground_group_members",
               return_value=[9999]), \
         patch("app.agents.tools.bash.os.kill", side_effect=lambda pid, sig: direct_kills.append((pid, sig))), \
         patch("app.agents.tools.bash.os.killpg",
               side_effect=killpg_side_effect):
        result = await _run_bash("proj", "ping x", timeout=2)

    assert killpg_targets == [(4242, signal.SIGINT)]
    assert direct_kills == [(9999, signal.SIGKILL)]
    assert "Command timed out after 2s" in result
    assert "exit code:" in result
    assert "stdout: " in result  # empty stdout section present


@pytest.mark.asyncio
async def test_run_bash_timeout_note_reports_cap():
    """A clamped timeout that runs out reports both the effective and the
    requested duration."""
    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.wait = AsyncMock()
    mock_proc.pid = 4242
    mock_proc.returncode = None

    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell",
               return_value=mock_proc), \
         patch("app.agents.tools.bash.os.killpg",
               side_effect=ProcessLookupError):  # group already gone
        result = await _run_bash("proj", "sleep x", timeout=MAX_TIMEOUT_SECONDS + 5)

    assert f"Command timed out after {MAX_TIMEOUT_SECONDS}s" in result
    assert f"capped at {MAX_TIMEOUT_SECONDS}s" in result


@pytest.mark.asyncio
async def test_run_bash_timeout_returns_even_if_wait_raises():
    """If post-kill proc.wait() itself raises, we still return the timeout
    format rather than propagating the exception (best-effort reap)."""

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    # wait() raises when the reap is attempted after SIGKILL
    mock_proc.wait = AsyncMock(side_effect=RuntimeError("reap failed"))
    mock_proc.pid = 4242  # real int: never rely on MagicMock's __index__
    mock_proc.returncode = None
    mock_proc.kill = MagicMock()

    # First wait_for (communicate) raises TimeoutError; second (proc.wait())
    # actually runs and surfaces the RuntimeError from the reap.
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell",
               return_value=mock_proc), \
         patch("app.agents.tools.bash.asyncio.wait_for",
               new=_raise_timeout_first_call()), \
         patch("app.agents.tools.bash.os.killpg",
               side_effect=ProcessLookupError):
        result = await _run_bash("proj", "ping x", timeout=1)

    assert "Command timed out after 1s" in result
    assert "exit code:" in result
    assert "stdout: " in result  # empty stdout section present


# ── _run_bash: real-subprocess timeout regression ───────────────────

@pytest.mark.asyncio
async def test_run_bash_timeout_returns_within_timeout_not_command_duration(
    project_root,
):
    """Regression: a timed-out command must return within ~timeout seconds,
    not wait for the command's own full duration.

    Previously the post-kill ``proc.communicate()`` blocked until the
    command's own timer expired (pipe EOF), so ``sleep 60`` with
    ``timeout=3`` took ~60s. This spawns a real subprocess (no mocks) and
    asserts the wall-clock elapsed time is bounded well below the command
    duration.
    """
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = await _run_bash("proj", "sleep 30", timeout=2)
    elapsed = loop.time() - t0

    assert "Command timed out after 2s" in result
    # Normal case: ~2s. If the bug regresses, elapsed ≈ 30s. The 15s bound
    # allows generous slack for CI load without masking a real regression.
    assert elapsed < 15, f"timeout did not bound elapsed time: {elapsed:.1f}s"


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 2])
async def test_normal_exit_leaves_background_group_alive(returncode):
    """A command that deliberately backgrounded jobs must not have them
    killed when the call ends: no signal is sent on the normal path."""
    process = MagicMock(pid=4242, returncode=returncode)
    process.communicate = AsyncMock(return_value=(b"output", b""))
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell", return_value=process), \
         patch("app.agents.tools.bash.os.killpg") as kill_group, \
         patch("app.agents.tools.bash.os.kill") as kill_pid:
        result = await _run_bash("proj", "background-command &")
    kill_group.assert_not_called()
    kill_pid.assert_not_called()
    assert f"exit code: {returncode}" in result


@pytest.mark.asyncio
async def test_communication_failure_still_cleans_process_and_pipes():
    process = MagicMock(pid=4242, returncode=None)
    process.communicate = AsyncMock(side_effect=OSError("pipe failed"))
    process.wait = AsyncMock()
    process.stdout._transport.is_closing.return_value = False
    process.stderr._transport.is_closing.return_value = False
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell", return_value=process), \
         patch("app.agents.tools.bash.os.killpg") as kill_group:
        result = await _run_bash("proj", "command")
    kill_group.assert_called_once_with(4242, signal.SIGINT)
    process.stdout._transport.close.assert_called_once()
    process.stderr._transport.close.assert_called_once()
    assert "pipe failed" in result
