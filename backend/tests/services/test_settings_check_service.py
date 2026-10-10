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

from app.core.config import LibrarySettings, ModelSettings, ModelRoleSettings, Settings
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


def _settings_with_rerank(*, reranker_enabled: bool, model: str = "") -> Settings:
    return Settings(
        models=ModelRoleSettings(
            supervisor=ModelSettings(
                model="gpt-4o", provider="openai", api_key="sk-test",
            ),
            rerank=ModelSettings(model=model),
        ),
        library=LibrarySettings(reranker_enabled=reranker_enabled),
    )


@pytest.mark.unit
@pytest.mark.regression
def test_enabled_rerank_without_model_is_not_skipped():
    """Enabled rerank with no model must run its check (and fail) instead of
    being silently skipped as 'Not configured'."""
    service = SettingsCheckService()
    cfg = _settings_with_rerank(reranker_enabled=True)
    assert service._should_skip(cfg, "rerank") is None


@pytest.mark.unit
@pytest.mark.regression
async def test_enabled_rerank_without_model_fails_check():
    service = SettingsCheckService()
    cfg = _settings_with_rerank(reranker_enabled=True)
    result = await service._run_model_check(cfg, "rerank")
    assert result["status"] == "fail"
    assert result["error_type"] == "config_error"
    assert "no rerank model" in result["message"]


@pytest.mark.unit
@pytest.mark.regression
async def test_enabled_rerank_without_model_fails_check_stream(monkeypatch):
    """The check stream reports enabled-but-unconfigured rerank as a failure."""
    real_check = SettingsCheckService._run_model_check

    async def only_rerank_is_real(self, cfg, role):
        if role == "rerank":
            return await real_check(self, cfg, role)
        return {"role": role, "label": role, "status": "pass"}
    monkeypatch.setattr(SettingsCheckService, "_run_model_check", only_rerank_is_real)

    config = {
        "models": {
            "supervisor": {
                "model": "gpt-4o", "provider": "openai", "api_key": "sk-test",
            },
            "rerank": {"model": "", "provider": ""},
        },
    }
    frames = [frame async for frame in SettingsCheckService().check(config=config)]
    events = _parse_events(frames)

    by_role = {
        data["role"]: data for event, data in events if event == "check_result"
    }
    assert by_role["rerank"]["status"] == "fail"
    assert by_role["rerank"]["error_type"] == "config_error"
    done = [data for event, data in events if event == "check_done"][0]
    assert done["failed"] == 1


@pytest.mark.unit
@pytest.mark.regression
def test_disabled_rerank_without_model_is_skipped():
    """A disabled reranker is intentionally off — skipped, not failed."""
    service = SettingsCheckService()
    cfg = _settings_with_rerank(reranker_enabled=False)
    assert service._should_skip(cfg, "rerank") == "Reranker disabled"
