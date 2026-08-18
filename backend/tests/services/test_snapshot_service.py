"""
Tests for the auto-snapshot trailing-timer behavior.

Regression background: a snapshot skipped inside the minimum-interval window
used to be dropped entirely, so the final state of a burst of edits was never
committed once the user stopped editing. The fix arms a one-shot re-check at
last-commit + interval.

Covers: the re-check commits once the interval elapses; timers are
deduplicated per project, re-armed for new commit windows, and released on
completion and shutdown (no task accumulation); a real-git end-to-end run.
"""

from datetime import timedelta

import pytest

import app.services.snapshot_service as snapshot_module
import app.core.config as config_module
from app.core.utils import utcnow
from app.services.snapshot_service import SnapshotService


class FakeConfigRepo:
    def __init__(self, values):
        self._values = values

    async def get(self, key, default=None):
        return self._values.get(key, default)


class FakeUoW:
    def __init__(self, values):
        self.config = FakeConfigRepo(values)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeGit:
    """Stateful git stand-in: pops one commit date per get_log call."""

    def __init__(self, dates, project_path):
        self._dates = list(dates)
        self._project_path = project_path
        self.commits = []

    def get_log(self, project_id, limit=50, offset=0, before=None):
        if not self._dates:
            return []
        return [{"date": self._dates.pop(0).isoformat()}]

    def get_project_path(self, project_id):
        return self._project_path

    def create_snapshot_commit(self, project_id):
        self.commits.append("Auto-snapshot")
        return {"success": True, "commit": "fakehash"}


@pytest.fixture
def fast_timers(monkeypatch, tmp_path):
    """Shrink the timer floor and keep lock files inside tmp_path."""
    monkeypatch.setattr(snapshot_module, "_MIN_TIMER_DELAY_SEC", 0.05)
    monkeypatch.setattr(config_module, "SIGMA_DIR", tmp_path / "locks")


def _patch_deps(monkeypatch, dates, tmp_path, values=None):
    values = values or {
        "snapshot_enabled": "true",
        "snapshot_interval_minutes": "1",
    }
    git = FakeGit(dates, tmp_path)
    monkeypatch.setattr(snapshot_module, "git_service", git)
    monkeypatch.setattr(snapshot_module, "UnitOfWork", lambda pid: FakeUoW(values))
    return git


async def test_skipped_snapshot_commits_when_interval_elapses(
    monkeypatch, tmp_path, fast_timers
):
    t0 = utcnow()
    # Both checks see the same backdated commit: the save-time check is
    # inside the window, the trailing fire lands past it.
    backdated = t0 - timedelta(seconds=59.95)
    git = _patch_deps(monkeypatch, [backdated, backdated], tmp_path)

    svc = SnapshotService()
    await svc.maybe_snapshot("p1")
    assert svc._pending.get("p1") is not None  # trailing re-check armed
    assert git.commits == []

    await svc._pending["p1"]

    assert len(git.commits) == 1  # final state committed
    assert svc._pending == {}  # entry released — no accumulation


async def test_pending_timer_is_deduplicated_and_shutdown_cancels(
    monkeypatch, tmp_path
):
    # Long floor keeps the timer sleeping so dedupe and shutdown can be
    # observed without racing the fire.
    monkeypatch.setattr(snapshot_module, "_MIN_TIMER_DELAY_SEC", 60.0)
    monkeypatch.setattr(config_module, "SIGMA_DIR", tmp_path / "locks")
    t0 = utcnow()
    git = _patch_deps(monkeypatch, [t0 - timedelta(seconds=30)] * 5, tmp_path)

    svc = SnapshotService()
    await svc.maybe_snapshot("p1")
    first = svc._pending["p1"]
    await svc.maybe_snapshot("p1")
    await svc.maybe_snapshot("p1")

    assert svc._pending["p1"] is first  # one timer per project
    assert git.commits == []

    await svc.shutdown()

    assert svc._pending == {}
    assert first.cancelled()


async def test_trailing_recheck_arms_fresh_timer_for_new_window(
    monkeypatch, tmp_path, fast_timers
):
    t0 = utcnow()
    # Save-time check: 59.95s elapsed -> skip, arm timer A.
    # Timer A fires: log now shows a 30s-old commit -> skip again and arm
    # timer B. This only works because A removes itself before re-checking.
    git = _patch_deps(
        monkeypatch,
        [t0 - timedelta(seconds=59.95), t0 - timedelta(seconds=30)],
        tmp_path,
    )

    svc = SnapshotService()
    await svc.maybe_snapshot("p1")
    timer_a = svc._pending["p1"]

    await timer_a

    timer_b = svc._pending.get("p1")
    assert timer_b is not None and timer_b is not timer_a
    assert git.commits == []  # still inside the new window

    await svc.shutdown()


async def test_disabled_snapshot_never_arms_timer(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module, "SIGMA_DIR", tmp_path / "locks")
    git = FakeGit([], tmp_path)
    monkeypatch.setattr(snapshot_module, "git_service", git)
    monkeypatch.setattr(
        snapshot_module,
        "UnitOfWork",
        lambda pid: FakeUoW({"snapshot_enabled": "false"}),
    )

    svc = SnapshotService()
    await svc.maybe_snapshot("p1")

    assert svc._pending == {}
    assert git.commits == []


async def test_future_commit_date_arms_capped_timer(
    monkeypatch, tmp_path, fast_timers
):
    # A commit date skewed into the future makes elapsed negative and the
    # remaining time exceed the full interval; the delay must stay capped.
    t0 = utcnow()
    _patch_deps(monkeypatch, [t0 + timedelta(hours=1), t0 + timedelta(hours=1)], tmp_path)

    svc = SnapshotService()
    await svc.maybe_snapshot("p1")

    assert svc._pending.get("p1") is not None

    await svc.shutdown()
    assert svc._pending == {}


@pytest.mark.integration
@pytest.mark.regression
@pytest.mark.timeout(30)
async def test_trailing_snapshot_commits_real_repo_after_interval(
    monkeypatch, tmp_path, fast_timers
):
    """End-to-end on a real repo: edits inside the interval window get their
    trailing snapshot even though no further save ever happens."""
    from app.services.git_service import GitService

    service = GitService()
    service.USERDATA_DIR = tmp_path
    project_id = "proj"
    project_dir = tmp_path / project_id
    project_dir.mkdir()
    (project_dir / "notes.md").write_text("v1\n", encoding="utf-8")
    assert service.init_git(project_id) is True  # creates the first commit

    (project_dir / "notes.md").write_text("v2 - final burst\n", encoding="utf-8")
    backdated = utcnow() - timedelta(seconds=59.95)

    class BackdatedGit:
        """Real git, except get_log reports the backdated first commit."""

        def get_log(self, project_id, limit=50, offset=0, before=None):
            return [{"date": backdated.isoformat()}]

        def __getattr__(self, name):
            return getattr(service, name)

    monkeypatch.setattr(snapshot_module, "git_service", BackdatedGit())
    _config = {
        "snapshot_enabled": "true",
        "snapshot_interval_minutes": "1",
    }
    monkeypatch.setattr(
        snapshot_module, "UnitOfWork", lambda pid: FakeUoW(_config)
    )

    svc = SnapshotService()
    await svc.maybe_snapshot(project_id)  # inside the window -> skipped

    assert len(service.get_log(project_id, 10)) == 1
    assert project_id in svc._pending

    await svc._pending[project_id]  # fire at last-commit + interval

    commits = service.get_log(project_id, 10)
    assert len(commits) == 2
    blob = service.get_blob(project_id, "notes.md", commits[0]["hash"])
    assert "v2 - final burst" in blob["content"]
    assert svc._pending == {}
