"""Regression tests for snapshot-pipeline failure immunity.

Background: a multi-GB dataset download inside a project made ``git add -A``
overrun its 30s subprocess timeout. The SIGKILL landed mid-operation and
orphaned ``.git/index.lock``; nothing healed it, so every later snapshot
failed instantly and silently for the rest of the day. These tests pin the
layered defences: graceful kill escalation, stale-lock healing, the
freshness gate, bounded lock acquisition, and persisted snapshot health.
"""

import json
import os
import subprocess
import time
from datetime import timedelta

import pytest

import app.core.config as config_module
import app.services.git_service as git_module
import app.services.snapshot_service as snapshot_module
from app.core.atomic_file import ProjectFileLock
from app.core.exceptions import FileSystemError
from app.core.utils import utcnow
from app.services.git_service import GitService, _run_subprocess_with_grace
from app.services.snapshot_service import SnapshotService


def _settle(path):
    """Backdate mtime beyond every freshness window used in these tests."""
    old = time.time() - 7200
    os.utime(path, (old, old))


def _make_repo(tmp_path):
    service = GitService()
    service.USERDATA_DIR = tmp_path
    (tmp_path / "proj").mkdir()
    service.init_git("proj")
    _settle(tmp_path / "proj" / ".gitignore")
    return service, tmp_path / "proj"


def _plant_lock(project_path, age_sec):
    lock = project_path / ".git" / "index.lock"
    lock.write_bytes(b"")
    old = time.time() - age_sec
    os.utime(lock, (old, old))
    return lock


@pytest.fixture
def local_locks(monkeypatch, tmp_path):
    """Keep ProjectFileLock sidecar files inside the test's tmp_path."""
    monkeypatch.setattr(config_module, "SIGMA_DIR", tmp_path / "locks")


# ---------------------------------------------------------------------------
# L1: graceful kill escalation
# ---------------------------------------------------------------------------

def test_subprocess_runner_returns_output_on_success():
    stdout, stderr, rc = _run_subprocess_with_grace(
        ["sh", "-c", "echo hi; echo err >&2"], timeout=10)
    assert stdout == b"hi\n"
    assert stderr == b"err\n"
    assert rc == 0


@pytest.mark.regression
@pytest.mark.timeout(20)
def test_subprocess_runner_terminates_term_responsive_process(monkeypatch):
    monkeypatch.setattr(git_module, "_TERM_GRACE_SEC", 0.5)
    start = time.monotonic()
    with pytest.raises(FileSystemError, match="timed out"):
        _run_subprocess_with_grace(
            ["sh", "-c", 'trap "exit 3" TERM; sleep 30'], timeout=0.3)
    assert time.monotonic() - start < 10


@pytest.mark.regression
@pytest.mark.timeout(20)
def test_subprocess_runner_kills_process_that_ignores_sigterm(monkeypatch):
    monkeypatch.setattr(git_module, "_TERM_GRACE_SEC", 0.5)
    start = time.monotonic()
    with pytest.raises(FileSystemError, match="timed out"):
        _run_subprocess_with_grace(
            ["sh", "-c", 'trap "" TERM; sleep 30'], timeout=0.3)
    assert time.monotonic() - start < 10


@pytest.mark.regression
@pytest.mark.timeout(60)
def test_timed_out_staging_leaves_repo_usable(tmp_path, monkeypatch):
    """The motivating incident in miniature: a staging command that overruns
    its budget is killed, and the repo must still accept the next staging —
    no wedged index.lock."""
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(git_module, "_TERM_GRACE_SEC", 1.0)
    # A clean filter that stalls makes `git add` genuinely slow, the way a
    # multi-GB dataset would.
    service._run_git("proj", ["config", "filter.stall.clean", "sleep 15; cat"])
    (project_path / ".gitattributes").write_text("*.bin filter=stall\n")
    (project_path / "big.bin").write_bytes(b"x" * 1024)

    monkeypatch.setattr(git_module, "GIT_WRITE_TIMEOUT_SEC", 0.5)
    with pytest.raises(FileSystemError, match="timed out"):
        service._run_git_add("proj", ["add", "-A"])

    monkeypatch.setattr(git_module, "GIT_WRITE_TIMEOUT_SEC", 600)
    service._run_git("proj", ["config", "filter.stall.clean", "cat"])
    assert service._run_git_add("proj", ["add", "-A"]) is True
    assert not (project_path / ".git" / "index.lock").exists()


# ---------------------------------------------------------------------------
# L2: stale index.lock healing
# ---------------------------------------------------------------------------

@pytest.mark.regression
@pytest.mark.timeout(30)
def test_stale_index_lock_removed_when_no_git_process_runs(
        tmp_path, monkeypatch):
    service, project_path = _make_repo(tmp_path)
    lock = _plant_lock(project_path, age_sec=120)
    monkeypatch.setattr(GitService, "_git_process_alive",
                        staticmethod(lambda path: False))

    assert service._heal_stale_index_lock(project_path) is True
    assert not lock.exists()


@pytest.mark.regression
@pytest.mark.timeout(30)
def test_index_lock_kept_when_young_or_git_process_alive(
        tmp_path, monkeypatch):
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(GitService, "_git_process_alive",
                        staticmethod(lambda path: False))

    young = _plant_lock(project_path, age_sec=5)
    assert service._heal_stale_index_lock(project_path) is False
    assert young.exists()

    old = _plant_lock(project_path, age_sec=120)
    monkeypatch.setattr(GitService, "_git_process_alive",
                        staticmethod(lambda path: True))
    assert service._heal_stale_index_lock(project_path) is False
    assert old.exists()


@pytest.mark.regression
@pytest.mark.timeout(30)
def test_git_add_heals_stale_lock_and_retries(tmp_path, monkeypatch):
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(GitService, "_git_process_alive",
                        staticmethod(lambda path: False))
    _plant_lock(project_path, age_sec=120)
    (project_path / "note.md").write_text("changed\n", encoding="utf-8")

    assert service._run_git_add("proj", ["add", "-A"]) is True
    assert not (project_path / ".git" / "index.lock").exists()


@pytest.mark.timeout(30)
def test_git_process_alive_detects_real_git_process(tmp_path):
    service, project_path = _make_repo(tmp_path)
    proc = subprocess.Popen(
        ["git", "--git-dir", str(project_path / ".git"), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    try:
        assert GitService._git_process_alive(project_path) is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert GitService._git_process_alive(project_path) is False


@pytest.mark.timeout(30)
def test_git_process_alive_matches_cwd_inside_worktree(tmp_path):
    """Agent-side git carries no repo path on its command line and may run
    from a worktree subdirectory — the cwd match must still find it."""
    service, project_path = _make_repo(tmp_path)
    sub = project_path / "sub"
    sub.mkdir()
    proc = subprocess.Popen(
        ["git", "cat-file", "--batch"], cwd=sub,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    try:
        assert GitService._git_process_alive(project_path) is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert GitService._git_process_alive(project_path) is False


# ---------------------------------------------------------------------------
# L3: freshness gate — auto snapshots wait for writes to settle, manual
# snapshots commit unconditionally
# ---------------------------------------------------------------------------

@pytest.mark.regression
@pytest.mark.timeout(30)
def test_auto_snapshot_defers_while_files_are_being_written(
        tmp_path, monkeypatch, local_locks):
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(git_module, "FRESH_FILE_SEC", 3600)
    (project_path / "download.part").write_bytes(b"x")

    result = service.create_snapshot_commit("proj", defer_unstable=True)

    assert result["success"] is False
    assert result["reason"] == "deferred"
    assert len(service.get_log("proj", 10)) == 1  # no commit was attempted


@pytest.mark.regression
@pytest.mark.timeout(30)
def test_auto_snapshot_commits_once_writes_settle(
        tmp_path, monkeypatch, local_locks):
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(git_module, "FRESH_FILE_SEC", 3600)
    (project_path / "download.part").write_bytes(b"x")
    _settle(project_path / "download.part")
    parent = service.get_log("proj", 1)[0]["hash"]

    assert service.create_snapshot_commit(
        "proj", defer_unstable=True)["success"] is True

    head = service.get_log("proj", 1)[0]["hash"]
    paths = [f["path"] for f in service.get_commit_files("proj", head, parent)]
    assert paths == ["download.part"]


@pytest.mark.regression
@pytest.mark.timeout(30)
def test_manual_snapshot_commits_fresh_files_unconditionally(
        tmp_path, monkeypatch, local_locks):
    """Manual commits keep the deterministic contract: a version right now,
    writes in flight or not."""
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(git_module, "FRESH_FILE_SEC", 3600)
    (project_path / "notes.md").write_text("just saved\n", encoding="utf-8")
    parent = service.get_log("proj", 1)[0]["hash"]

    assert service.create_snapshot_commit("proj")["success"] is True

    head = service.get_log("proj", 1)[0]["hash"]
    paths = [f["path"] for f in service.get_commit_files("proj", head, parent)]
    assert paths == ["notes.md"]


@pytest.mark.timeout(30)
def test_worktree_stability_honours_ignore_rules(tmp_path, monkeypatch):
    """Writes into a user-ignored directory (dataset scratch) must not hold
    snapshots back — the gate sees exactly what `git add -A` would stage."""
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(git_module, "FRESH_FILE_SEC", 3600)
    scratch = project_path / "datasets"
    scratch.mkdir()
    gitignore = project_path / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "datasets/\n",
        encoding="utf-8")
    _settle(gitignore)
    (scratch / "download.part").write_bytes(b"x")

    assert service._worktree_is_stable("proj", project_path) is True

    (project_path / "real.txt").write_text("now", encoding="utf-8")
    assert service._worktree_is_stable("proj", project_path) is False


@pytest.mark.timeout(30)
def test_worktree_stability_ignores_sigma_state_churn(
        tmp_path, monkeypatch):
    """Even if a user un-ignores .SiGMA, its constantly-rewritten DB must
    never read as an unstable worktree."""
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(git_module, "FRESH_FILE_SEC", 3600)
    gitignore = project_path / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8").replace(".SiGMA/\n", ""),
        encoding="utf-8")
    _settle(gitignore)
    sigma = project_path / ".SiGMA"
    sigma.mkdir()
    (sigma / "project_data.db").write_bytes(b"db")

    assert service._worktree_is_stable("proj", project_path) is True


@pytest.mark.timeout(30)
def test_worktree_stability_clamps_future_mtime(tmp_path, monkeypatch):
    """A file dated into the future (clock skew) must not read as
    forever-fresh and defer snapshots indefinitely."""
    service, project_path = _make_repo(tmp_path)
    monkeypatch.setattr(git_module, "FRESH_FILE_SEC", 3600)
    skewed = project_path / "skewed.txt"
    skewed.write_text("from the future", encoding="utf-8")
    future = time.time() + 3600
    os.utime(skewed, (future, future))

    assert service._worktree_is_stable("proj", project_path) is True


@pytest.mark.regression
def test_generated_gitignore_gains_cache_rule(tmp_path):
    service, project_path = _make_repo(tmp_path)
    gitignore = project_path / ".gitignore"
    assert ".cache/" in gitignore.read_text(encoding="utf-8")  # template rule

    stripped = gitignore.read_text(encoding="utf-8").replace(".cache/\n", "", 1)
    gitignore.write_text(stripped, encoding="utf-8")
    assert service._ensure_generated_gitignore(project_path) is True
    assert ".cache/" in gitignore.read_text(encoding="utf-8")

    # Idempotent, and user-authored ignore files are never touched.
    assert service._ensure_generated_gitignore(project_path) is False
    gitignore.write_text("node_modules/\n", encoding="utf-8")
    assert service._ensure_generated_gitignore(project_path) is False
    assert gitignore.read_text(encoding="utf-8") == "node_modules/\n"


@pytest.mark.timeout(30)
def test_startup_maintenance_sweeps_all_repos(tmp_path, monkeypatch):
    service, project_path = _make_repo(tmp_path)
    (tmp_path / "not-a-repo").mkdir()
    monkeypatch.setattr(GitService, "_git_process_alive",
                        staticmethod(lambda path: False))
    _plant_lock(project_path, age_sec=120)
    gitignore = project_path / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8").replace(".cache/\n", "", 1),
        encoding="utf-8",
    )

    counts = service.startup_maintenance()

    assert counts == {"gitignore_updated": 1, "stale_locks_removed": 1}
    assert not (project_path / ".git" / "index.lock").exists()


# ---------------------------------------------------------------------------
# L4: bounded lock acquisition
# ---------------------------------------------------------------------------

@pytest.mark.regression
@pytest.mark.timeout(30)
def test_snapshot_lock_acquisition_is_bounded(tmp_path, local_locks):
    index = tmp_path / "proj" / ".git" / "index"

    with ProjectFileLock(index):
        with pytest.raises(TimeoutError):
            ProjectFileLock(index, timeout=0.3).__enter__()

    # Released lock is acquirable again.
    with ProjectFileLock(index, timeout=0.3):
        pass


# ---------------------------------------------------------------------------
# L5: snapshot health persistence + service flow
# ---------------------------------------------------------------------------

@pytest.fixture
def recording_config(monkeypatch):
    """UnitOfWork stand-in that records config writes for inspection."""
    store = {
        "snapshot_enabled": "true",
        "snapshot_interval_minutes": "1",
    }

    class ConfigRepo:
        async def get(self, key, default=None):
            return store.get(key, default)

        async def set(self, key, value):
            store[key] = value

    class UoW:
        config = ConfigRepo()

        def __init__(self, project_id):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(snapshot_module, "UnitOfWork", UoW)
    return store


async def test_record_health_counts_consecutive_failures(recording_config):
    svc = SnapshotService()

    await svc.record_health("p1", {"status": "error", "error": "boom"})
    await svc.record_health("p1", {"status": "error", "error": "boom"})
    health = json.loads(recording_config["snapshot_health"])
    assert health["consecutive_failures"] == 2
    assert health["status"] == "error"
    assert health["last_error"] == "boom"

    await svc.record_health("p1", {"status": "committed"})
    health = json.loads(recording_config["snapshot_health"])
    assert health["consecutive_failures"] == 0
    assert health["last_success_at"] is not None


async def test_deferral_is_not_a_failure(recording_config):
    """Deferrals are normal during active work; they must not trip the
    failure banner."""
    svc = SnapshotService()

    await svc.record_health("p1", {"status": "error", "error": "x"})
    await svc.record_health("p1", {"status": "deferred"})

    health = json.loads(recording_config["snapshot_health"])
    assert health["status"] == "deferred"
    assert health["consecutive_failures"] == 1


async def test_noop_outcome_leaves_health_untouched(recording_config):
    svc = SnapshotService()
    await svc.record_health("p1", {"status": "error", "error": "x"})
    before = recording_config.get("snapshot_health")

    await svc.record_health("p1", {"status": "noop"})

    assert recording_config.get("snapshot_health") == before


async def test_get_health_defaults_to_healthy(recording_config):
    svc = SnapshotService()

    assert await svc.get_health("p1") == {
        "status": "ok",
        "consecutive_failures": 0,
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error": None,
    }


class StubGit:
    """git_service stand-in: history says the interval has elapsed."""

    def __init__(self, last_commit_at):
        self._last_commit_at = last_commit_at

    def get_log(self, project_id, limit=50, offset=0, before=None):
        return [{"date": self._last_commit_at.isoformat()}]


class DeferredGit(StubGit):
    def create_snapshot_commit(self, project_id, defer_unstable=False):
        return {"success": False, "reason": "deferred",
                "detail": "files still being written"}


class ExplodingGit(StubGit):
    def create_snapshot_commit(self, project_id, defer_unstable=False):
        raise FileSystemError("Git add -A failed", code="INTERNAL_ERROR")


@pytest.mark.regression
@pytest.mark.timeout(10)
async def test_maybe_snapshot_deferral_arms_fast_retry(
        monkeypatch, recording_config):
    monkeypatch.setattr(snapshot_module, "_DEFERRED_RETRY_SEC", 0.05)
    monkeypatch.setattr(snapshot_module, "git_service",
                        DeferredGit(utcnow() - timedelta(minutes=10)))

    svc = SnapshotService()
    await svc.maybe_snapshot("p1")

    health = json.loads(recording_config["snapshot_health"])
    assert health["status"] == "deferred"
    assert svc._pending.get("p1") is not None  # short-delay retry armed
    await svc.shutdown()


@pytest.mark.regression
async def test_maybe_snapshot_records_git_failure(
        monkeypatch, recording_config):
    monkeypatch.setattr(snapshot_module, "git_service",
                        ExplodingGit(utcnow() - timedelta(minutes=10)))

    svc = SnapshotService()
    await svc.maybe_snapshot("p1")

    health = json.loads(recording_config["snapshot_health"])
    assert health["status"] == "error"
    assert health["consecutive_failures"] == 1
