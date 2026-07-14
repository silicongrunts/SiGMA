"""Tests for the permission routes (auto-approve settings only).

Permission approval no longer uses a dedicated HTTP endpoint. When the LLM
agent needs user approval, the task is parked as ``awaiting_input`` and the
user's response flows back through the chat resume path
(``POST /chat/stream`` with ``resume=true``). This module only tests the
auto-approve GET/PUT endpoints.
"""

from types import SimpleNamespace

import pytest

from app.core.exceptions import ServiceException
from app.routes import permissions


@pytest.mark.route
@pytest.mark.asyncio
async def test_set_auto_approve_rejects_invalid_category(monkeypatch):
    """An invalid category raises ServiceException with a 400 status."""
    monkeypatch.setattr(
        permissions,
        "project_service",
        SimpleNamespace(get_project_path=lambda project_id: "/project"),
    )
    with pytest.raises(ServiceException) as exc_info:
        await permissions.set_auto_approve(
            "project-1",
            type("Req", (), {"category": "invalid_cat", "enabled": True})(),
        )
    assert exc_info.value.code == "PERMISSION_INVALID_CATEGORY"
    assert exc_info.value.status_code == 400


@pytest.mark.route
@pytest.mark.asyncio
async def test_set_auto_approve_passes_valid_category(monkeypatch):
    """A valid category is forwarded to project_service.set_auto_approve."""
    calls = []

    async def fake_set_auto_approve(pid, cat, enabled):
        calls.append({"project_id": pid, "category": cat, "enabled": enabled})

    monkeypatch.setattr(
        permissions,
        "project_service",
        SimpleNamespace(
            get_project_path=lambda project_id: "/project",
            set_auto_approve=fake_set_auto_approve,
        ),
    )
    result = await permissions.set_auto_approve(
        "project-1",
        type("Req", (), {"category": "bash", "enabled": True})(),
    )
    assert result["data"] == {"category": "bash", "enabled": True}
    assert calls == [{"project_id": "project-1", "category": "bash", "enabled": True}]
