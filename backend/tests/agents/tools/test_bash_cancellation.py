"""Bash tool cancellation must kill the whole subprocess group.

Cancelling the in-flight ``_run_bash`` task kills the shell's process
group, so a spawned long-running child cannot outlive the request.
"""

import asyncio
import os
import signal
import subprocess
import sys
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.tools import bash as bash_module
from app.agents.tools.bash import _run_bash


def _procs_containing(marker: str) -> list:
    """PIDs whose cmdline contains *marker* (avoids pgrep's self-match)."""
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\0", b" ").decode(
                    "utf-8", errors="replace")
        except OSError:
            continue
        if marker in cmdline:
            hits.append((pid, cmdline))
    return hits


def _pgid_of(pid: str) -> int:
    """Process group id from /proc/<pid>/stat (field 5)."""
    with open(f"/proc/{pid}/stat") as f:
        # comm may contain spaces/parens — parse after the last ')'.
        return int(f.read().rsplit(")", 1)[1].split()[2])


def _pids_in_pgid(pgid: int) -> list:
    hits = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if _pgid_of(pid) == pgid:
                hits.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return hits


@pytest.mark.asyncio
async def test_bash_cancellation_kills_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bash_module, "settings",
        SimpleNamespace(get_project_path=lambda pid: tmp_path))

    marker = "sigma_cancel_probe_9d41"
    task = asyncio.create_task(
        _run_bash("proj", f"sleep 300; echo {marker}", timeout=600))
    await asyncio.sleep(0.5)
    # The shell (and its sleep child) must be running before cancellation.
    shells = _procs_containing(marker)
    assert shells, "command did not start"
    pgid = _pgid_of(shells[0][0])
    assert len(_pids_in_pgid(pgid)) >= 2, "child process not in group"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The whole process group must be dead — killing only the coroutine (or
    # only the shell) would orphan the sleep child.
    await asyncio.sleep(0.3)
    assert not _pids_in_pgid(pgid), \
        "bash process group survived task cancellation"


def _is_running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Linux process lifecycle")
async def test_normal_exit_leaves_redirected_background_child_running(tmp_path, monkeypatch):
    """A job the command deliberately backgrounded (with redirected output)
    must survive the tool call returning."""
    monkeypatch.setattr(bash_module, "settings", SimpleNamespace(get_project_path=lambda _: tmp_path))
    result = await _run_bash("proj", "sleep 30 </dev/null >/dev/null 2>&1 & echo $!", timeout=5)
    assert "exit code: 0" in result, result
    child_pid = int(result.split("stdout: ", 1)[1].splitlines()[0])
    assert child_pid > 1
    try:
        assert _is_running(child_pid), "backgrounded child was killed on normal exit"
    finally:
        if _is_running(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Linux signal semantics")
async def test_timeout_kills_foreground_but_spares_background_child(tmp_path, monkeypatch):
    """On timeout the foreground command dies while a backgrounded job —
    which POSIX starts with SIGINT ignored — keeps running."""
    monkeypatch.setattr(bash_module, "settings", SimpleNamespace(get_project_path=lambda _: tmp_path))
    result = await _run_bash("proj", "sleep 120 </dev/null >/dev/null 2>&1 & sleep 300", timeout=2)
    assert "Command timed out after 2s" in result, result
    survivors = [pid for pid, _ in _procs_containing("sleep 120") if _is_running(int(pid))]
    try:
        assert not _procs_containing("sleep 300") or not any(
            _is_running(int(pid)) for pid, _ in _procs_containing("sleep 300")
        ), "foreground command survived the timeout"
        assert survivors, "backgrounded child was killed by the timeout"
    finally:
        for pid, _ in _procs_containing("sleep 120"):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signal")
async def test_web_process_crash_terminates_active_shell_group(tmp_path):
    ready = tmp_path / "ready"
    command = f"sleep 60 & echo $! > {shlex.quote(str(ready))}; wait"
    source = (
        "import asyncio, sys; from pathlib import Path; from types import SimpleNamespace; "
        "sys.path.insert(0, sys.argv[1]); "
        "from app.agents.tools import bash; "
        "bash.settings = SimpleNamespace(get_project_path=lambda _: Path(sys.argv[2])); "
        "asyncio.run(bash._run_bash('project', sys.argv[3], timeout=60))"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", source, str(Path(__file__).resolve().parents[3]), str(tmp_path), command],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert parent.pid > 1
    child_pid = None
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while not ready.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert ready.exists()
        child_pid = int(ready.read_text().strip())
        assert child_pid > 1
        assert _is_running(child_pid)
        parent.kill()
        parent.wait(timeout=5)
        deadline = asyncio.get_running_loop().time() + 3
        while _is_running(child_pid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert not _is_running(child_pid)
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if child_pid is not None and _is_running(child_pid):
            os.kill(child_pid, signal.SIGKILL)
