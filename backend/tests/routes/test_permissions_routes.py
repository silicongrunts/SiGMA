from types import SimpleNamespace

import pytest

from app.core.exceptions import ServiceException
from app.models.requests import PermissionRespondRequest
from app.routes import permissions


@pytest.mark.route
@pytest.mark.asyncio
async def test_respond_permission_returns_approval_when_worker_receives(monkeypatch):
    calls = []

    async def respond_permission(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        permissions,
        "project_service",
        SimpleNamespace(get_project_path=lambda project_id: calls.append({"project_id": project_id})),
    )
    monkeypatch.setattr(
        permissions,
        "stream_server",
        SimpleNamespace(respond_permission=respond_permission),
    )

    result = await permissions.respond_permission(
        "project-1",
        "task-1",
        PermissionRespondRequest(request_id="req-1", approved=False, reason=None),
    )

    assert result["data"] == {"request_id": "req-1", "approved": False}
    assert calls == [
        {"project_id": "project-1"},
        {
            "task_id": "task-1",
            "request_id": "req-1",
            "approved": False,
            "reason": "",
        },
    ]


@pytest.mark.route
@pytest.mark.asyncio
async def test_respond_permission_maps_missing_task(monkeypatch):
    async def respond_permission(**kwargs):
        return False

    monkeypatch.setattr(
        permissions,
        "project_service",
        SimpleNamespace(get_project_path=lambda project_id: "/project"),
    )
    monkeypatch.setattr(
        permissions,
        "stream_server",
        SimpleNamespace(respond_permission=respond_permission),
    )

    with pytest.raises(ServiceException) as exc_info:
        await permissions.respond_permission(
            "project-1",
            "task-1",
            PermissionRespondRequest(request_id="req-1", approved=True),
        )

    assert exc_info.value.code == "PERMISSION_TASK_NOT_FOUND"
    assert exc_info.value.status_code == 404
