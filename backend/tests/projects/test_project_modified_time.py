"""
Tests for project modified-time tracking and list ordering.

``touch_project`` is the hook content services call (file saves, notebook
writes, chat turns, annotations, library changes) so the homepage project
list ordering reflects real user activity.
"""

import json

import pytest

from app.services.project_service import ProjectService, PROJECT_STATUS_DELETING


@pytest.fixture
def ps(tmp_path):
    """Create a ProjectService pointing at a temp directory."""
    svc = ProjectService()
    svc.USERDATA_DIR = tmp_path
    svc.SIGMA_DIR = tmp_path / ".SiGMA"
    svc.SIGMA_DIR.mkdir(parents=True, exist_ok=True)
    svc.PROJECTS_FILE = svc.SIGMA_DIR / "projects.json"
    return svc


def _add_project(ps, pid, modified=None, status=None):
    (ps.USERDATA_DIR / pid).mkdir()
    entry = {"name": pid}
    if modified:
        entry["modified"] = modified
    if status:
        entry["status"] = status
    ps._update_projects(lambda p: p.update({pid: entry}))


# ---------------------------------------------------------------------------
# touch_project
# ---------------------------------------------------------------------------

def test_touch_updates_modified(ps):
    """Touching an active project replaces a stale modified timestamp."""
    _add_project(ps, "p1", modified="2020-01-01T00:00:00Z")

    ps.touch_project("p1")

    data = json.loads(ps.PROJECTS_FILE.read_text())
    assert data["p1"]["modified"] != "2020-01-01T00:00:00Z"
    assert data["p1"]["modified"]  # non-empty ISO timestamp


def test_touch_missing_project_is_noop(ps):
    """Touching an unknown project neither raises nor rewrites the index."""
    _add_project(ps, "p1", modified="2020-01-01T00:00:00Z")
    before = ps.PROJECTS_FILE.read_text()

    ps.touch_project("unknown")

    assert ps.PROJECTS_FILE.read_text() == before


def test_touch_inactive_project_is_noop(ps):
    """Touching a non-active project leaves its timestamp unchanged."""
    _add_project(ps, "p1", modified="2020-01-01T00:00:00Z",
                 status=PROJECT_STATUS_DELETING)

    ps.touch_project("p1")

    data = json.loads(ps.PROJECTS_FILE.read_text())
    assert data["p1"]["modified"] == "2020-01-01T00:00:00Z"


def test_touch_survives_corrupt_index(ps):
    """A corrupt index is logged and swallowed, not raised to the caller."""
    _add_project(ps, "p1")
    ps.PROJECTS_FILE.write_text("{bad json!!", encoding="utf-8")

    ps.touch_project("p1")  # must not raise


# ---------------------------------------------------------------------------
# list_projects ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_projects_most_recent_first(ps, monkeypatch):
    """list_projects returns projects ordered by modified, newest first."""
    async def _fake_config(self, project_id):
        return {}

    monkeypatch.setattr(ProjectService, "_get_config", _fake_config)
    _add_project(ps, "old", modified="2020-01-01T00:00:00Z")
    _add_project(ps, "newest", modified="2024-06-01T12:00:00Z")
    _add_project(ps, "middle", modified="2022-03-01T00:00:00Z")
    _add_project(ps, "untouched")  # no modified timestamp

    listed = await ps.list_projects()

    assert [p["id"] for p in listed] == ["newest", "middle", "old", "untouched"]
