from app.services.jupyter_service import JupyterService


def test_jupyter_config_lives_under_sigma_dir(tmp_path, monkeypatch):
    from app.core import config
    import app.services.jupyter_service as jupyter_module

    monkeypatch.setattr(config, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SIGMA_DIR", tmp_path / ".SiGMA")
    settings = config.Settings()
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(jupyter_module, "settings", settings)

    svc = JupyterService(base_dir=str(settings.USERDATA_DIR), port=8899)
    svc._write_config()

    assert svc.base_dir == settings.USERDATA_DIR
    assert svc.runtime_dir == settings.SIGMA_DIR / "jupyter"
    assert svc._config_path == settings.SIGMA_DIR / "jupyter" / "jupyter_server_config.py"
    assert svc._config_path.exists()
    config_text = svc._config_path.read_text(encoding="utf-8")
    assert f"c.ServerApp.root_dir = '{settings.USERDATA_DIR}'" in config_text
    assert f"c.IdentityProvider.token = '{svc.token}'" in config_text
    assert "c.NotebookApp." not in config_text
    assert "exposeAppInBrowser" not in config_text


def test_jupyter_service_starts_nbclassic(tmp_path, monkeypatch):
    from app.core import config
    import app.services.jupyter_service as jupyter_module

    monkeypatch.setattr(config, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SIGMA_DIR", tmp_path / ".SiGMA")
    settings = config.Settings()
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(jupyter_module, "settings", settings)

    svc = JupyterService(base_dir=str(settings.USERDATA_DIR), port=8899)
    svc._write_config()

    assert svc._start_command()[:2] == [settings.JUPYTER_BIN, "nbclassic"]
    assert f"--config={svc._config_path}" in svc._start_command()


def test_jupyter_embedded_url_uses_classic_notebook_route(tmp_path, monkeypatch):
    from app.core import config
    import app.services.jupyter_service as jupyter_module

    monkeypatch.setattr(config, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SIGMA_DIR", tmp_path / ".SiGMA")
    settings = config.Settings()
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(jupyter_module, "settings", settings)

    svc = JupyterService(base_dir=str(settings.USERDATA_DIR), port=8899)

    assert svc.get_url("project/analysis.ipynb").startswith(
        "/api/v1/jupyter/notebooks/project/analysis.ipynb?token="
    )


def test_browser_data_dir_lives_under_sigma_dir(tmp_path, monkeypatch):
    from app.core import config
    import app.services.browser_service as browser_module

    monkeypatch.setattr(config, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SIGMA_DIR", tmp_path / ".SiGMA")
    settings = config.Settings()
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(browser_module, "settings", settings)
    monkeypatch.setattr(browser_module, "browser_service", None)

    svc = browser_module.get_browser_service()

    assert svc.base_dir == settings.SIGMA_DIR
    assert settings.SIGMA_DIR / "browser_data" == svc.base_dir / "browser_data"


def test_huey_db_lives_under_sigma_huey_dir(tmp_path, monkeypatch):
    from app.core import config
    import app.workers.huey_config as huey_config

    monkeypatch.setattr(config, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SIGMA_DIR", tmp_path / ".SiGMA")
    settings = config.Settings()

    huey_db = huey_config._get_huey_db_path(settings.SIGMA_DIR)

    assert huey_db == settings.SIGMA_DIR / "huey" / "huey.db"
    assert huey_db.parent.exists()
