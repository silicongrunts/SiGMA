"""Bash tool cancellation must kill the whole subprocess group.

Cancelling the in-flight ``_run_bash`` task kills the shell's process
group, so a spawned long-running child cannot outlive the request.
"""

import asyncio
import os
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
