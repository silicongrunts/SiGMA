import logging
from datetime import timedelta

import pytest

from app.core.logging import setup_logging
from app.core.utils import utcnow


@pytest.fixture(autouse=True)
def reset_root_logging():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    original_process = getattr(root, "_sigma_logging_process", None)

    for handler in root.handlers[:]:
        root.removeHandler(handler)
    if hasattr(root, "_sigma_logging_process"):
        delattr(root, "_sigma_logging_process")

    yield

    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)
    if original_process is not None:
        root._sigma_logging_process = original_process  # type: ignore[attr-defined]
    elif hasattr(root, "_sigma_logging_process"):
        delattr(root, "_sigma_logging_process")


def test_setup_logging_writes_daily_file_and_splits_console(tmp_path, capsys):
    setup_logging(process="web", log_dir=tmp_path, level="INFO")

    logger = logging.getLogger("sigma.test")
    logger.debug("debug file only")
    logger.info("hello stdout")
    logger.warning("hello stderr")

    captured = capsys.readouterr()
    assert "debug file only" not in captured.out
    assert "debug file only" not in captured.err
    assert "hello stdout" in captured.out
    assert "hello stderr" not in captured.out
    assert "hello stderr" in captured.err

    today = utcnow().date().isoformat()
    log_file = tmp_path / f"web-{today}.log"
    content = log_file.read_text(encoding="utf-8")
    assert "debug file only" in content
    assert "hello stdout" in content
    assert "hello stderr" in content


def test_setup_logging_is_idempotent_without_force(tmp_path):
    setup_logging(process="worker", log_dir=tmp_path, level="INFO")
    root = logging.getLogger()
    first_handlers = root.handlers[:]

    setup_logging(process="web", log_dir=tmp_path, level="INFO")

    assert root.handlers == first_handlers
    assert getattr(root, "_sigma_logging_process") == "worker"


def test_setup_logging_force_reconfigures_process(tmp_path):
    setup_logging(process="worker", log_dir=tmp_path, level="INFO")
    setup_logging(process="web", log_dir=tmp_path, level="INFO", force=True)

    logging.getLogger("sigma.test").info("web log")

    today = utcnow().date().isoformat()
    assert (tmp_path / f"web-{today}.log").read_text(encoding="utf-8")
    assert not (tmp_path / f"worker-{today}.log").exists()
    assert getattr(logging.getLogger(), "_sigma_logging_process") == "web"


def test_setup_logging_removes_expired_logs(tmp_path):
    old_date = (utcnow().date() - timedelta(days=14)).isoformat()
    kept_date = (utcnow().date() - timedelta(days=13)).isoformat()
    old_log = tmp_path / f"worker-{old_date}.log"
    kept_log = tmp_path / f"worker-{kept_date}.log"
    old_log.write_text("old", encoding="utf-8")
    kept_log.write_text("kept", encoding="utf-8")

    setup_logging(process="worker", log_dir=tmp_path)

    assert not old_log.exists()
    assert kept_log.exists()


def test_setup_logging_uses_settings_defaults(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SIGMA_DIR", tmp_path / ".SiGMA")
    settings = config.Settings.model_validate({
        "logging": {"level": "WARNING", "retention_days": 3},
    })
    monkeypatch.setattr(config, "settings", settings)

    old_date = (utcnow().date() - timedelta(days=3)).isoformat()
    old_log = tmp_path / ".SiGMA" / "logs" / f"web-{old_date}.log"
    old_log.parent.mkdir(parents=True)
    old_log.write_text("old", encoding="utf-8")

    setup_logging(process="web")

    logger = logging.getLogger("sigma.test")
    logger.info("hidden from console but kept in file")
    logger.warning("visible warning")

    today = utcnow().date().isoformat()
    content = (tmp_path / ".SiGMA" / "logs" / f"web-{today}.log").read_text(encoding="utf-8")
    assert "hidden from console but kept in file" in content
    assert "visible warning" in content
    assert not old_log.exists()
