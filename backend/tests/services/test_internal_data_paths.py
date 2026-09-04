"""Internal data paths of the Jupyter/Browser daemons.

Both daemons derive their data/config locations from the config settings
object, so every test runs against a fresh ``Settings`` whose userdata root
lives under ``tmp_path``.
"""

import pytest

from app.services.jupyter_service import JupyterService


@pytest.fixture
def rooted_settings(tmp_path, monkeypatch):
    """Fresh Settings whose USERDATA_DIR/SIGMA_DIR live under tmp_path.

    The config module's globals are patched so ``Settings()`` and any
    module-level reads pick up the temp root; consumers that imported
    ``settings`` directly patch their own module attribute with the
    returned instance.
    """
    from app.core import config
    monkeypatch.setattr(config, "USERDATA_DIR", tmp_path)
    monkeypatch.setattr(config, "SIGMA_DIR", tmp_path / ".SiGMA")
    settings = config.Settings()
    monkeypatch.setattr(config, "settings", settings)
    return settings


def test_jupyter_config_lives_under_sigma_dir(rooted_settings, monkeypatch):
    import app.services.jupyter_service as jupyter_module
    monkeypatch.setattr(jupyter_module, "settings", rooted_settings)

    svc = JupyterService(base_dir=str(rooted_settings.USERDATA_DIR), port=8899)
    svc._write_config()

    assert svc.base_dir == rooted_settings.USERDATA_DIR
    assert svc.runtime_dir == rooted_settings.SIGMA_DIR / "jupyter"
    assert svc._config_path == rooted_settings.SIGMA_DIR / "jupyter" / "jupyter_server_config.py"
    assert svc._config_path.exists()
    config_text = svc._config_path.read_text(encoding="utf-8")
    assert f"c.ServerApp.root_dir = '{rooted_settings.USERDATA_DIR}'" in config_text
    assert f"c.IdentityProvider.token = '{svc.token}'" in config_text
    assert "c.NotebookApp." not in config_text
    assert "exposeAppInBrowser" not in config_text


def test_jupyter_service_starts_nbclassic(rooted_settings, monkeypatch):
    import app.services.jupyter_service as jupyter_module
    monkeypatch.setattr(jupyter_module, "settings", rooted_settings)

    svc = JupyterService(base_dir=str(rooted_settings.USERDATA_DIR), port=8899)
    svc._write_config()

    assert svc._start_command()[:2] == [rooted_settings.JUPYTER_BIN, "nbclassic"]
    assert f"--config={svc._config_path}" in svc._start_command()


def test_jupyter_embedded_url_uses_classic_notebook_route(rooted_settings):
    svc = JupyterService(base_dir=str(rooted_settings.USERDATA_DIR), port=8899)

    assert svc.get_url("project/analysis.ipynb").startswith(
        "/api/v1/jupyter/notebooks/project/analysis.ipynb?token="
    )


def test_browser_data_dir_lives_under_sigma_dir(rooted_settings, monkeypatch):
    import app.services.browser_service as browser_module
    monkeypatch.setattr(browser_module, "settings", rooted_settings)
    monkeypatch.setattr(browser_module, "browser_service", None)

    svc = browser_module.get_browser_service()

    assert svc.base_dir == rooted_settings.SIGMA_DIR
