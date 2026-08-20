"""
Tests for the settings structure check and required-field rules.

Regression background: a half-configured optional role (draw with a provider
selected but no model name) used to fail the structure check with
"Missing required fields: draw model" and abort every later connectivity
check. Optional roles must stay skippable; only the supervisor model is a
hard requirement.
"""

import json

import pytest

from app.core.config import ModelSettings, ModelRoleSettings, Settings
from app.services.settings_check_service import SettingsCheckService


def _settings_with_draw_provider() -> Settings:
    return Settings(models=ModelRoleSettings(
        supervisor=ModelSettings(
            model="gpt-4o", provider="openai", api_key="sk-test",
        ),
        draw=ModelSettings(model="", provider="openai"),
    ))


def _parse_events(frames: list[str]) -> list[tuple[str, dict]]:
    parsed = []
    for frame in frames:
        event_line, data_line = frame.strip().split("\n")
        event = event_line.removeprefix("event: ")
        data = json.loads(data_line.removeprefix("data: "))
        parsed.append((event, data))
    return parsed


@pytest.mark.unit
@pytest.mark.regression
def test_half_configured_optional_role_is_not_required():
    """A provider without a model name must not count as a missing field."""
    service = SettingsCheckService()
    cfg = _settings_with_draw_provider()
    assert service._check_required_fields(cfg) == []
    assert service._should_skip(cfg, "draw") == "Not configured"


@pytest.mark.unit
def test_missing_supervisor_model_is_required():
    service = SettingsCheckService()
    cfg = Settings(models=ModelRoleSettings(
        supervisor=ModelSettings(model="", provider="openai"),
    ))
    assert service._check_required_fields(cfg) == ["supervisor model"]


@pytest.mark.unit
@pytest.mark.regression
async def test_check_stream_passes_structure_with_half_configured_draw(monkeypatch):
    """Structure check passes and draw is skipped instead of failing the run."""

    async def fake_model_check(self, cfg, role):
        return {"role": role, "label": role, "status": "pass"}

    monkeypatch.setattr(SettingsCheckService, "_run_model_check", fake_model_check)

    config = {
        "models": {
            "supervisor": {
                "model": "gpt-4o", "provider": "openai", "api_key": "sk-test",
            },
            "draw": {"model": "", "provider": "openai"},
        },
    }
    frames = [frame async for frame in SettingsCheckService().check(config=config)]
    events = _parse_events(frames)

    by_role = {
        data["role"]: data for event, data in events if event == "check_result"
    }
    assert by_role["structure"]["status"] == "pass"
    assert by_role["draw"]["status"] == "skip"
    assert by_role["draw"]["reason"] == "Not configured"
    done = [data for event, data in events if event == "check_done"][0]
    assert done["failed"] == 0


@pytest.mark.unit
async def test_check_stream_fails_structure_without_supervisor():
    config = {"models": {"supervisor": {"model": "", "provider": "openai"}}}
    frames = [frame async for frame in SettingsCheckService().check(config=config)]
    events = _parse_events(frames)

    by_role = {
        data["role"]: data for event, data in events if event == "check_result"
    }
    assert by_role["structure"]["status"] == "fail"
    assert "supervisor model" in by_role["structure"]["message"]
    done = [data for event, data in events if event == "check_done"][0]
    assert done["failed"] == 1
