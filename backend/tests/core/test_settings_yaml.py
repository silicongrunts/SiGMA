from pathlib import Path

import pytest
import yaml

from app.core.config import Settings, dump_settings_yaml, load_settings_file, save_settings_yaml


def test_settings_yaml_round_trip(tmp_path: Path):
    config = Settings()
    config.models.supervisor.model = "gpt-test"
    config.models.supervisor.provider = "openai"
    config.models.supervisor.api_key = "sk-test"

    path = tmp_path / "settings.yaml"
    path.write_text(dump_settings_yaml(config), encoding="utf-8")

    loaded = load_settings_file(path)

    assert loaded.models.supervisor.model == "gpt-test"
    assert loaded.models.supervisor.provider == "openai"
    assert loaded.models.supervisor.api_key == "sk-test"
    # Two representative defaults prove unrelated sections survive the
    # round trip. Not every default constant gets its own pin: plain scalar
    # fields with no serialization or migration subtleties (e.g.
    # RAG_CANDIDATE_POOL_SIZE, LIBRARY_QUEUE_BATCH_SIZE, RETRY_DELAY,
    # RETRY_MAX_DELAY) are intentionally left unpinned — the round-trip
    # guarantee is structural, and pinning each default here would only
    # duplicate config.py.
    assert loaded.LOG_LEVEL == "INFO"
    assert loaded.MAX_RETRIES == 10
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

    # The assertions read through model_settings_for_role: the test's point
    # is that reuse resolves to the supervisor endpoint's values.
    assert config.model_settings_for_role("ra").model == "claude-sonnet"
    assert config.model_settings_for_role("vision").provider == "anthropic"


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


def test_settings_yaml_accepts_background_cleanup_timeout(tmp_path: Path):
    config = Settings.model_validate({
        "background": {"library_scan_total_timeout_seconds": 90},
    })

    assert config.LIBRARY_SCAN_TOTAL_TIMEOUT_SECONDS == 90

    # Round-trips through the canonical dump.
    path = tmp_path / "settings.yaml"
    path.write_text(dump_settings_yaml(config), encoding="utf-8")
    assert load_settings_file(path).LIBRARY_SCAN_TOTAL_TIMEOUT_SECONDS == 90


def test_settings_yaml_rejects_invalid_background_cleanup_timeout():
    with pytest.raises(ValueError):
        Settings.model_validate({
            "background": {"library_scan_total_timeout_seconds": 0},
        })


def test_settings_yaml_rejects_invalid_logging_config():
    with pytest.raises(ValueError):
        Settings.model_validate({"logging": {"level": "verbose"}})

    with pytest.raises(ValueError):
        Settings.model_validate({"logging": {"retention_days": 0}})


def test_load_settings_file_migrates_workers_atomically_and_idempotently(tmp_path: Path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "workers:\n"
        "  library_workers: 3\n"
        "  task_cleanup_hours: 9\n",
        encoding="utf-8",
    )

    loaded = load_settings_file(path)
    assert loaded.LIBRARY_CONCURRENCY == 3
    assert loaded.BACKGROUND_TASK_CLEANUP_HOURS == 9
    migrated = path.read_text(encoding="utf-8")
    assert "workers" not in yaml.safe_load(migrated)

    assert load_settings_file(path).LIBRARY_CONCURRENCY == 3
    assert path.read_text(encoding="utf-8") == migrated


def test_load_settings_file_migration_preserves_explicit_background_values(tmp_path: Path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "workers:\n"
        "  library_workers: 3\n"
        "background:\n"
        "  library_concurrency: 7\n",
        encoding="utf-8",
    )

    loaded = load_settings_file(path)

    assert loaded.LIBRARY_CONCURRENCY == 7


def test_invalid_settings_migration_leaves_original_file_unchanged(tmp_path: Path):
    path = tmp_path / "settings.yaml"
    original = "workers:\n  library_workers: invalid\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError):
        load_settings_file(path)

    assert path.read_text(encoding="utf-8") == original
