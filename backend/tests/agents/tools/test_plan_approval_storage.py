from datetime import datetime
from types import SimpleNamespace

import pytest

from app.agents.tools import plan_approval_tool


@pytest.mark.asyncio
async def test_approved_plan_is_saved_under_session_temp_dir(tmp_path, monkeypatch):
    fake_settings = SimpleNamespace(get_project_path=lambda project_id: tmp_path)
    monkeypatch.setattr(
        plan_approval_tool,
        "settings",
        fake_settings,
    )
    monkeypatch.setattr(
        "app.services.session_temp_service.settings",
        fake_settings,
    )
    monkeypatch.setattr(plan_approval_tool, "generate_id", lambda: "abcdef1234567890")
    monkeypatch.setattr(
        plan_approval_tool,
        "utcnow",
        lambda: datetime(2026, 6, 30, 0, 0, 0),
    )

    path = await plan_approval_tool._save_plan(
        "project-a",
        "session-1",
        "Plan body\x00\nNext line",
    )

    assert path == ".SiGMA/sessions/session-1/plans/20260630-000000-abcdef.md"
    saved = tmp_path / path
    assert saved.read_text(encoding="utf-8") == "Plan body\nNext line"


@pytest.mark.asyncio
async def test_approved_plan_exposes_relative_path_for_compaction(tmp_path, monkeypatch):
    fake_settings = SimpleNamespace(get_project_path=lambda project_id: tmp_path)
    monkeypatch.setattr(
        plan_approval_tool,
        "settings",
        fake_settings,
    )
    monkeypatch.setattr(
        "app.services.session_temp_service.settings",
        fake_settings,
    )

    result = await plan_approval_tool._submit_plan_for_approval_phase2(
        plan_content="Plan body",
        approved=True,
        project_id="project-a",
        session_id="session-1",
    )

    # The relative path is intentionally exposed so compaction summaries can
    # remember where the approved plan file lives.
    assert "internal session temporary storage" in result
    assert ".SiGMA/sessions/session-1/plans/" in result
    assert (tmp_path / ".SiGMA" / "sessions" / "session-1" / "plans").is_dir()
