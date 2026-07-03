from types import SimpleNamespace

import pytest

from app.routes import terminal


@pytest.mark.route
@pytest.mark.asyncio
async def test_list_terminal_sessions_wraps_sessions(monkeypatch):
    def list_project_sessions(project_id):
        assert project_id == "project-1"
        return [{"session_id": "term-1", "slot": 1, "state": "ACTIVE"}]

    monkeypatch.setattr(
        terminal,
        "terminal_service",
        SimpleNamespace(list_project_sessions=list_project_sessions),
    )

    result = await terminal.list_sessions("project-1")

    assert result["data"] == {
        "sessions": [{"session_id": "term-1", "slot": 1, "state": "ACTIVE"}]
    }
