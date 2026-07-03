from pathlib import Path

import pytest

from app.core.config import Settings, dump_settings_yaml, load_settings_file, save_settings_yaml


def test_settings_yaml_round_trip(tmp_path: Path):
    config = Settings()
    config.models.supervisor.model = "gpt-test"
    config.models.supervisor.provider = "openai"
    config.models.supervisor.api_key = "sk-test"

    path = tmp_path / "settings.yaml"
    path.write_text(dump_settings_yaml(config), encoding="utf-8")

    loaded = load_settings_file(path)

    assert loaded.SUPERVISOR_MODEL == "gpt-test"
    assert loaded.SUPERVISOR_PROVIDER == "openai"
    assert loaded.SUPERVISOR_API_KEY == "sk-test"
    assert loaded.RAG_CANDIDATE_POOL_SIZE == 15
    assert loaded.LIBRARY_WORKERS == 1
    assert loaded.LIBRARY_QUEUE_BATCH_SIZE == 20
    assert loaded.LOG_LEVEL == "INFO"
    assert loaded.LOG_RETENTION_DAYS == 14
    assert loaded.MAX_RETRIES == 10
    assert loaded.RETRY_DELAY == 2.0
    assert loaded.RETRY_MAX_DELAY == 64.0
    assert "stream:" not in path.read_text(encoding="utf-8")


def test_invalid_settings_yaml_is_not_written(tmp_path: Path):
    path = tmp_path / "settings.yaml"
    original = dump_settings_yaml(Settings())
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError):
        save_settings_yaml("models: []", path)

    assert path.read_text(encoding="utf-8") == original


def test_settings_yaml_allows_supported_model_reuse():
    config = Settings.model_validate({
        "models": {
            "supervisor": {
                "model": "claude-sonnet",
                "provider": "anthropic",
                "api_key": "sk-supervisor",
            },
            "ra": {"reuse": "supervisor"},
            "vision": {"reuse": "ra"},
        },
    })

    assert config.RA_MODEL == "claude-sonnet"
    assert config.VISION_PROVIDER == "anthropic"


def test_settings_yaml_rejects_unsupported_model_reuse():
    with pytest.raises(ValueError):
        Settings.model_validate({
            "models": {
                "supervisor": {"reuse": "ra"},
            },
        })

    with pytest.raises(ValueError):
        Settings.model_validate({
            "models": {
                "ra": {"reuse": "vision"},
            },
        })


def test_settings_yaml_accepts_logging_config():
    config = Settings.model_validate({
        "logging": {
            "level": "debug",
            "retention_days": 30,
        },
    })

    assert config.LOG_LEVEL == "DEBUG"
    assert config.LOG_RETENTION_DAYS == 30


def test_settings_yaml_rejects_invalid_logging_config():
    with pytest.raises(ValueError):
        Settings.model_validate({"logging": {"level": "verbose"}})

    with pytest.raises(ValueError):
        Settings.model_validate({"logging": {"retention_days": 0}})
