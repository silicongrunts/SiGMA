"""Shared fixtures for project registry / lifecycle tests."""

import pytest

from app.services.project_service import ProjectService


@pytest.fixture
def ps(tmp_path, monkeypatch):
    """A ProjectService whose userdata root is a temporary directory.

    The module-level ``USERDATA_DIR`` is patched to the same temp root so
    every consumer of the setting (project_service, core.project_registry,
    database manager) stays inside the sandbox, per the RULES/TESTING.md
    isolation rules.
    """
    from app.core import config as config_module

    monkeypatch.setattr(config_module, "USERDATA_DIR", tmp_path)

    svc = ProjectService()
    svc.USERDATA_DIR = tmp_path
    svc.SIGMA_DIR = tmp_path / ".SiGMA"
    svc.SIGMA_DIR.mkdir(parents=True, exist_ok=True)
    svc.PROJECTS_FILE = svc.SIGMA_DIR / "projects.json"
    return svc
