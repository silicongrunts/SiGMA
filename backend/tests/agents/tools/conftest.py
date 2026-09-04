"""Shared fixtures for agent tool contract tests."""

from types import SimpleNamespace

import pytest


@pytest.fixture
def fake_settings(tmp_path):
    """Settings stub that resolves every project to ``tmp_path``."""
    return SimpleNamespace(get_project_path=lambda project_id: tmp_path)


@pytest.fixture
def project_root(tmp_path, monkeypatch, fake_settings):
    """Isolate every project-path seam the tools consult to ``tmp_path``.

    Tool modules reach the project root through different module-level
    singletons (their own ``settings`` import, ``file_service``,
    ``session_temp_service``). Patching them all here keeps each test
    focused on *what* it exercises instead of which module's path lookup
    needs patching. Returns the project root (``tmp_path``).
    """
    from app.agents.tools import bash as bash_module
    from app.agents.tools import notebook_utils
    from app.agents.tools import plan_approval_tool
    from app.services import file_service
    from app.services import session_temp_service

    monkeypatch.setattr(bash_module, "settings", fake_settings)
    monkeypatch.setattr(notebook_utils, "settings", fake_settings)
    monkeypatch.setattr(plan_approval_tool, "settings", fake_settings)
    monkeypatch.setattr(session_temp_service, "settings", fake_settings)
    monkeypatch.setattr(
        file_service.file_service,
        "get_project_path",
        lambda project_id: tmp_path,
    )
    return tmp_path
