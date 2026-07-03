from types import SimpleNamespace

import pytest

from app.services.session_temp_service import SessionTempService


def test_session_temp_dir_lives_under_project_sigma_sessions(tmp_path, monkeypatch):
    import app.services.session_temp_service as temp_module

    monkeypatch.setattr(
        temp_module,
        "settings",
        SimpleNamespace(get_project_path=lambda project_id: tmp_path),
    )

    service = SessionTempService()
    target = service.ensure_session_dir("project-a", "session-1")

    assert target == tmp_path / ".SiGMA" / "sessions" / "session-1"
    assert target.is_dir()


@pytest.mark.parametrize("session_id", ["", "../escape", "bad/name", "bad name"])
def test_session_temp_dir_rejects_invalid_session_ids(tmp_path, monkeypatch, session_id):
    import app.services.session_temp_service as temp_module

    monkeypatch.setattr(
        temp_module,
        "settings",
        SimpleNamespace(get_project_path=lambda project_id: tmp_path),
    )

    with pytest.raises(ValueError):
        SessionTempService().ensure_session_dir("project-a", session_id)


def test_delete_session_dir_removes_only_session_temp_dir(tmp_path, monkeypatch):
    import app.services.session_temp_service as temp_module

    monkeypatch.setattr(
        temp_module,
        "settings",
        SimpleNamespace(get_project_path=lambda project_id: tmp_path),
    )

    service = SessionTempService()
    session_dir = service.ensure_session_dir("project-a", "session-1")
    keep_dir = service.ensure_session_dir("project-a", "session-2")
    (session_dir / "scratch.md").write_text("temporary", encoding="utf-8")
    (keep_dir / "scratch.md").write_text("keep", encoding="utf-8")

    service.delete_session_dir("project-a", "session-1")

    assert not session_dir.exists()
    assert (keep_dir / "scratch.md").exists()
