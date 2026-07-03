from types import SimpleNamespace

import pytest

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
