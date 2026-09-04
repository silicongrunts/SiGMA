"""Tests for the permission routes (auto-approve settings only).

Permission approval has no dedicated HTTP endpoint: when the agent needs user
approval, the task is parked as ``awaiting_input`` and the user's response
flows back through the chat resume path (``POST /chat/stream`` with
``resume=true`` and ``interaction_response``). This module covers the
auto-approve GET/PUT endpoints, including the per-category validation.
"""

from types import SimpleNamespace

import pytest

from app.core.exceptions import ServiceException
from app.models.requests import AutoApproveUpdate
from app.routes import permissions


@pytest.mark.route
@pytest.mark.asyncio
async def test_get_auto_approve_returns_project_flags(monkeypatch):
    """GET validates the project, then forwards to the service and wraps the
    per-category flags in the unified envelope."""
    calls = []

    async def get_auto_approve(project_id):
        calls.append(("get", project_id))
        return {"bash": True, "file_internal": False}

    # The gate and the delegated service call share one project_service, so
    # the shared project_gate fixture (gate-only) does not apply here.
    monkeypatch.setattr(
        permissions,
        "project_service",
        SimpleNamespace(
            get_project_path=lambda project_id: calls.append(("project", project_id)),
            get_auto_approve=get_auto_approve,
        ),
    )

    result = await permissions.get_auto_approve("project-1")

    assert result["data"] == {"bash": True, "file_internal": False}
    assert calls == [("project", "project-1"), ("get", "project-1")]


@pytest.mark.route
@pytest.mark.asyncio
async def test_set_auto_approve_rejects_invalid_category(monkeypatch, project_gate):
    """An unknown category raises ServiceException with a 400 status before
    any project lookup or persistence happens."""
    calls = []
    project_gate(monkeypatch, permissions, calls)

    with pytest.raises(ServiceException) as exc_info:
        await permissions.set_auto_approve(
            "project-1",
            AutoApproveUpdate(category="invalid_cat", enabled=True),
        )

    assert exc_info.value.code == "PERMISSION_INVALID_CATEGORY"
    assert exc_info.value.status_code == 400
    assert calls == []  # rejected before the project validation


@pytest.mark.route
@pytest.mark.asyncio
async def test_set_auto_approve_passes_valid_category(monkeypatch):
    """A valid category is forwarded to project_service.set_auto_approve."""
    calls = []

    async def fake_set_auto_approve(pid, cat, enabled):
        calls.append({"project_id": pid, "category": cat, "enabled": enabled})

    # Same reason as above: gate + delegated call on one project_service.
    monkeypatch.setattr(
        permissions,
        "project_service",
        SimpleNamespace(
            get_project_path=lambda project_id: calls.append(("project", project_id)),
            set_auto_approve=fake_set_auto_approve,
        ),
    )
    result = await permissions.set_auto_approve(
        "project-1",
        AutoApproveUpdate(category="bash", enabled=True),
    )
    assert result["data"] == {"category": "bash", "enabled": True}
    assert calls == [
        ("project", "project-1"),
        {"project_id": "project-1", "category": "bash", "enabled": True},
    ]
