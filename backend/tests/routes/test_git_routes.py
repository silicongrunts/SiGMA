from types import SimpleNamespace

import pytest

from app.models.requests import CreateTagRequest
from app.routes import git


@pytest.mark.route
@pytest.mark.asyncio
async def test_get_log_enforces_route_defaults_to_service(monkeypatch):
    calls = {}

    def get_log(project_id, limit, offset, before):
        calls["log"] = (project_id, limit, offset, before)
        return [{"hash": "abc"}]

    monkeypatch.setattr(git, "git_service", SimpleNamespace(get_log=get_log))

    result = await git.get_log("project-1", limit=50, offset=0, before=None)

    assert result["data"] == {"commits": [{"hash": "abc"}]}
    assert calls["log"] == ("project-1", 50, 0, None)


@pytest.mark.route
@pytest.mark.asyncio
async def test_get_diff_passes_default_resolution_inputs(monkeypatch):
    calls = {}

    def get_diff_with_defaults(project_id, path, commit, short_hash, parent_commit):
        calls["diff"] = (project_id, path, commit, short_hash, parent_commit)
        return {"diff": "..."}

    monkeypatch.setattr(
        git,
        "git_service",
        SimpleNamespace(get_diff_with_defaults=get_diff_with_defaults),
    )

    result = await git.get_diff(
        "project-1",
        path="paper.tex",
        commit=None,
        parent_commit="parent",
        short_hash="abc",
    )

    assert result["data"] == {"diff": "..."}
    assert calls["diff"] == ("project-1", "paper.tex", None, "abc", "parent")


@pytest.mark.route
@pytest.mark.asyncio
async def test_tag_routes_delegate_to_service(monkeypatch):
    calls = {}

    def list_tags(project_id):
        calls["list"] = project_id
        return [{"name": "v1", "commit": "abcd123", "short_hash": "abcd123"}]

    def create_tag(project_id, name, commit):
        calls["create"] = (project_id, name, commit)
        return {"success": True, "name": name, "commit": commit}

    def delete_tag(project_id, name):
        calls["delete"] = (project_id, name)
        return {"success": True, "name": name}

    monkeypatch.setattr(git, "git_service", SimpleNamespace(
        list_tags=list_tags, create_tag=create_tag, delete_tag=delete_tag,
    ))

    listed = await git.list_tags("project-1")
    assert listed["data"] == {"tags": [{"name": "v1", "commit": "abcd123", "short_hash": "abcd123"}]}
    created = await git.create_tag("project-1", request=CreateTagRequest(name="v2", commit="abcd124"))
    assert created["data"] == {"success": True, "name": "v2", "commit": "abcd124"}
    deleted = await git.delete_tag("project-1", "v1")
    assert deleted["data"] == {"success": True, "name": "v1"}

    assert calls["list"] == "project-1"
    assert calls["create"] == ("project-1", "v2", "abcd124")
    assert calls["delete"] == ("project-1", "v1")


@pytest.mark.route
@pytest.mark.asyncio
async def test_manual_commit_delegates_to_service(monkeypatch):
    calls = {}

    async def commit_now(project_id):
        calls["commit"] = project_id
        return {"success": False, "reason": "no changes"}

    monkeypatch.setattr(git, "snapshot_service", SimpleNamespace(
        commit_now=commit_now,
    ))

    result = await git.manual_commit("project-1")

    assert result["data"] == {"success": False, "reason": "no changes"}
    assert calls["commit"] == "project-1"


@pytest.mark.route
@pytest.mark.asyncio
async def test_snapshot_health_returns_persisted_state(monkeypatch):
    async def get_health(project_id):
        return {"status": "error", "consecutive_failures": 2}

    monkeypatch.setattr(git, "snapshot_service", SimpleNamespace(
        get_health=get_health,
    ))

    result = await git.snapshot_health("project-1")

    assert result["data"] == {"status": "error", "consecutive_failures": 2}


@pytest.mark.route
@pytest.mark.asyncio
async def test_repair_heals_lock_then_commits(monkeypatch):
    calls = []

    def heal_stale_lock(project_id):
        calls.append("heal")
        return {"lock_removed": True}

    async def commit_now(project_id):
        calls.append("commit")
        return {"success": True, "commit": "abc1234"}

    monkeypatch.setattr(git, "git_service", SimpleNamespace(
        heal_stale_lock=heal_stale_lock,
    ))
    monkeypatch.setattr(git, "snapshot_service", SimpleNamespace(
        commit_now=commit_now,
    ))

    result = await git.repair_snapshot("project-1")

    assert result["data"] == {
        "lock_removed": True,
        "commit": {"success": True, "commit": "abc1234"},
    }
    assert calls == ["heal", "commit"]  # healing happens before the retry
