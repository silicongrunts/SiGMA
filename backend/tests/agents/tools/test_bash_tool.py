"""
Unit tests for the bash tool.

Covers:
- ``_format_output`` produces the unified three-section format in all cases
- ``_run_bash`` validates timeout, kills timed-out subprocesses, returns the
  timeout note promptly (without blocking for the command's full duration),
  and uses the unified format on success/failure/timeout.
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


# ── _run_bash: timeout kills subprocess ─────────────────────────────

@pytest.mark.asyncio
async def test_run_bash_timeout_kills_and_returns_timeout_note():
    """On timeout: proc.kill() must be called and the unified format must be
    returned with the timeout note. The post-kill path no longer drains
    pipes (it uses proc.wait()), so partial output is intentionally not
    captured — see test_run_bash_timeout_returns_within_timeout_not_command_duration
    for the wall-clock regression test."""

    captured_signals = {"killed": False}

    async def slow_communicate():
        # Simulate a long-running process; wait_for will cancel this
        await asyncio.sleep(10)
        return b"", b""

    def kill_side_effect():
        captured_signals["killed"] = True

    mock_proc = MagicMock()
    mock_proc.communicate = slow_communicate
    mock_proc.returncode = -9
    mock_proc.kill = MagicMock(side_effect=kill_side_effect)

    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell",
               return_value=mock_proc), \
         patch("app.agents.tools.bash.asyncio.wait_for",
               new=_raise_timeout):
        result = await _run_bash("proj", "ping x", timeout=2)

    assert captured_signals["killed"] is True
    assert "exit code: -9  (Command timed out after 2s)" in result
    assert "stdout: " in result  # empty stdout section present


@pytest.mark.asyncio
async def test_run_bash_timeout_returns_even_if_wait_raises():
    """If post-kill proc.wait() itself raises, we still return the timeout
    format rather than propagating the exception (best-effort reap)."""

    mock_proc = MagicMock()
    mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    # wait() raises when the reap is attempted after SIGKILL
    mock_proc.wait = AsyncMock(side_effect=RuntimeError("reap failed"))
    mock_proc.returncode = None
    mock_proc.kill = MagicMock()

    # First wait_for (communicate) raises TimeoutError; second (proc.wait())
    # actually runs and surfaces the RuntimeError from the reap.
    with patch("app.agents.tools.bash.asyncio.create_subprocess_shell",
               return_value=mock_proc), \
         patch("app.agents.tools.bash.asyncio.wait_for",
               new=_raise_timeout_first_call()):
        result = await _run_bash("proj", "ping x", timeout=1)

    assert "Command timed out after 1s" in result
    assert "exit code:" in result
    assert "stdout: " in result  # empty stdout section present


# ── _run_bash: real-subprocess timeout regression ───────────────────

@pytest.mark.asyncio
async def test_run_bash_timeout_returns_within_timeout_not_command_duration(
    tmp_path, monkeypatch
):
    """Regression: a timed-out command must return within ~timeout seconds,
    not wait for the command's own full duration.

    Previously the post-kill ``proc.communicate()`` blocked until the
    command's own timer expired (pipe EOF), so ``sleep 60`` with
    ``timeout=3`` took ~60s. This spawns a real subprocess (no mocks) and
    asserts the wall-clock elapsed time is bounded well below the command
    duration.
    """
    from types import SimpleNamespace
    from app.agents.tools import bash as bash_mod
    monkeypatch.setattr(
        bash_mod, "settings",
        SimpleNamespace(get_project_path=lambda pid: tmp_path),
    )

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = await _run_bash("proj", "sleep 30", timeout=2)
    elapsed = loop.time() - t0

    assert "Command timed out after 2s" in result
    # Normal case: ~2s. If the bug regresses, elapsed ≈ 30s. The 15s bound
    # allows generous slack for CI load without masking a real regression.
    assert elapsed < 15, f"timeout did not bound elapsed time: {elapsed:.1f}s"
