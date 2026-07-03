"""
Unit tests for the bash tool.

Covers:
- ``_format_output`` produces the unified three-section format in all cases
- ``_run_bash`` validates timeout, kills timed-out subprocesses, returns
  partial output on timeout, and uses the unified format on success/failure.
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.tools.bash import MAX_TIMEOUT_SECONDS, _format_output, _run_bash


async def _raise_timeout(awaitable, timeout):
    if inspect.iscoroutine(awaitable):
        awaitable.close()
    raise asyncio.TimeoutError


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
async def test_run_bash_rejects_timeout_above_max():
    result = await _run_bash("proj", "ls", timeout=MAX_TIMEOUT_SECONDS + 1)
    assert "Error: timeout" in result
    assert "invalid" in result
    assert str(MAX_TIMEOUT_SECONDS + 1) in result


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


# ── _run_bash: timeout kills subprocess and captures partial output ──

@pytest.mark.asyncio
async def test_run_bash_timeout_kills_and_returns_partial():
    """On timeout: proc.kill() must be called, partial output drained from
    pipes, and the unified format returned with the timeout note."""

    captured_signals = {"killed": False}

    async def slow_communicate():
        # Simulate a long-running process; wait_for will cancel this
        await asyncio.sleep(10)
        return b"", b""

    async def drain_communicate():
        # Subsequent communicate() after kill returns the partial output
        return b"partial stdout line\n", b""

    def kill_side_effect():
        captured_signals["killed"] = True
        # Switch communicate to the drain version
        mock_proc.communicate = drain_communicate

    mock_proc = MagicMock()
    mock_proc.communicate = slow_communicate
    mock_proc.returncode = -15
    mock_proc.kill = MagicMock(side_effect=kill_side_effect)

    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell",
               return_value=mock_proc), \
         patch("app.agents.tools.bash.asyncio.wait_for",
               new=_raise_timeout):
        result = await _run_bash("proj", "ping x", timeout=2)

    assert captured_signals["killed"] is True
    assert "exit code: -15  (Command timed out after 2s)" in result
    assert "stdout: partial stdout line" in result


@pytest.mark.asyncio
async def test_run_bash_timeout_drain_failure_falls_back_to_empty():
    """If post-kill drain itself raises, we still return the timeout format
    with empty output rather than propagating the exception."""

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_proc.returncode = -15
    mock_proc.kill = MagicMock()

    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell",
               return_value=mock_proc), \
         patch("app.agents.tools.bash.asyncio.wait_for",
               new=_raise_timeout):
        result = await _run_bash("proj", "ping x", timeout=1)

    assert "Command timed out after 1s" in result
    assert "stdout: " in result  # empty stdout section present
