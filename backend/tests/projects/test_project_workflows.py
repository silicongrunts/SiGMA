"""
Tests for public project workflows (create, get, update, delete).

Uses a temporary ProjectService with mocked DB/config access to verify
projects.json layer correctness and return values.
"""

import json
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from app.core.exceptions import DatabaseIncompatibleError
from app.services.project_service import ProjectService


@pytest.fixture
def ps(tmp_path):
    """Create a ProjectService pointing at a temp directory."""
    svc = ProjectService()
    svc.USERDATA_DIR = tmp_path
    svc.SIGMA_DIR = tmp_path / ".SiGMA"
    svc.SIGMA_DIR.mkdir(parents=True, exist_ok=True)
    svc.PROJECTS_FILE = svc.SIGMA_DIR / "projects.json"
    return svc


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {"main_file": "", "engine": "pdflatex", "template": "latex"}


def _mock_get_config(config=None):
    """Return an async method stub for _get_config."""
    cfg = config or _DEFAULT_CONFIG

    async def _get_config(self, project_id):
        return dict(cfg)
    return _get_config


async def _noop_set_config(self, project_id, config):
    pass


# ---------------------------------------------------------------------------
# create_project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_create_project_returns_correct_data(ps):
    """create_project returns a well-formed dict with no NameError."""
    with (
        patch.object(ProjectService, '_get_config', _mock_get_config()),
        patch.object(ProjectService, '_set_config', _noop_set_config),
    ):
        result = await ps.create_project("Test Project", "A description", "latex")

    assert "id" in result
    assert result["name"] == "Test Project"
    assert result["description"] == "A description"
    assert "created" in result
    assert "modified" in result


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_create_project_persists_to_json(ps):
    """create_project writes metadata to projects.json."""
    with (
        patch.object(ProjectService, '_get_config', _mock_get_config()),
        patch.object(ProjectService, '_set_config', _noop_set_config),
    ):
        result = await ps.create_project("My Project")

    data = json.loads(ps.PROJECTS_FILE.read_text())
    pid = result["id"]
    assert pid in data
    assert data[pid]["name"] == "My Project"
    assert data[pid]["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_create_project_creates_directory(ps):
    """create_project creates the project directory on disk."""
    with (
        patch.object(ProjectService, '_get_config', _mock_get_config()),
        patch.object(ProjectService, '_set_config', _noop_set_config),
    ):
        result = await ps.create_project("Dir Test")

    project_path = ps.USERDATA_DIR / result["id"]
    assert project_path.is_dir()


# ---------------------------------------------------------------------------
# update_project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_update_project_modifies_json(ps):
    """update_project writes updated name to projects.json."""
    with (
        patch.object(ProjectService, '_get_config', _mock_get_config()),
        patch.object(ProjectService, '_set_config', _noop_set_config),
    ):
        created = await ps.create_project("Original")
        pid = created["id"]

        updated = await ps.update_project(pid, {"name": "Renamed"})

    assert updated["name"] == "Renamed"
    data = json.loads(ps.PROJECTS_FILE.read_text())
    assert data[pid]["name"] == "Renamed"


# ---------------------------------------------------------------------------
# delete_project — test only the _update_projects delete path
#
# delete_project() touches UnitOfWork, DB manager, RAG cache, Jupyter,
# stream_server etc.  Rather than mocking all of those, we test the
# projects.json mutation directly via _update_projects, which is the
# only part that changed in this round.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_update_projects_delete_removes_entry(ps):
    """_update_projects can delete a project entry atomically."""
    # Seed two projects
    (ps.USERDATA_DIR / "p1").mkdir()
    (ps.USERDATA_DIR / "p2").mkdir()
    ps._update_projects(lambda p: p.update({
        "p1": {"name": "One"},
        "p2": {"name": "Two"},
    }))

    # Delete p1
    def _delete_p1(projects):
        del projects["p1"]
    ps._update_projects(_delete_p1)

    data = json.loads(ps.PROJECTS_FILE.read_text())
    assert "p1" not in data
    assert data["p2"]["name"] == "Two"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_list_projects_hides_deleted_entries(ps):
    """Deleted registry tombstones are not returned as active projects."""
    (ps.USERDATA_DIR / "p1").mkdir()
    (ps.USERDATA_DIR / "p2").mkdir()
    ps._update_projects(lambda p: p.update({
        "p1": {"name": "One", "status": "deleted"},
        "p2": {"name": "Two", "status": "active"},
    }))

    with patch.object(ProjectService, '_get_config', _mock_get_config()):
        projects = await ps.list_projects()

    assert [p["id"] for p in projects] == ["p2"]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_finalize_import_keeps_project_when_db_is_incompatible(ps):
    """A SiGMA-project import keeps files even when its bundled DB is incompatible."""
    project_id = "imported-bad-db"
    (ps.USERDATA_DIR / project_id).mkdir()

    mock_db = MagicMock()
    mock_db.unmark_deleted = MagicMock()
    mock_db.ensure_db_exists = AsyncMock(
        side_effect=DatabaseIncompatibleError("newer revision")
    )

    with (
        patch("app.database.manager.get_db_manager", new_callable=AsyncMock, return_value=mock_db),
        patch.object(ProjectService, '_get_config', _mock_get_config()),
    ):
        result = await ps._finalize_new_project(
            project_id,
            "Imported",
            "desc",
            init_git=False,
            config=None,
        )

    mock_db.unmark_deleted.assert_called_once_with(project_id)
    mock_db.ensure_db_exists.assert_awaited_once_with(project_id)
    assert result["id"] == project_id
    assert result["name"] == "Imported"

    data = json.loads(ps.PROJECTS_FILE.read_text())
    assert data[project_id]["status"] == "active"
    assert (ps.USERDATA_DIR / project_id).is_dir()
