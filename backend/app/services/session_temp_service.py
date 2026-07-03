"""Session-scoped temporary storage under a project's internal data directory."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.core.utils import is_within


logger = get_logger(__name__)

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SessionTempService:
    """Own paths and lifecycle for hidden per-session temporary storage."""

    def session_dir(self, project_id: str, session_id: str) -> Path:
        clean_session_id = self._validate_session_id(session_id)
        project_path = Path(settings.get_project_path(project_id)).resolve()
        target = project_path / ".SiGMA" / "sessions" / clean_session_id
        self._assert_inside_project(target, project_path)
        return target

    def ensure_session_dir(self, project_id: str, session_id: str) -> Path:
        target = self.session_dir(project_id, session_id)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def session_dir_for_prompt(self, project_id: str, session_id: str) -> str:
        return str(self.ensure_session_dir(project_id, session_id))

    def delete_session_dir(self, project_id: str, session_id: str) -> None:
        target = self.session_dir(project_id, session_id)
        if not target.exists():
            return
        if not target.is_dir():
            logger.warning("Session temp path is not a directory: %s", target)
            return
        shutil.rmtree(target)

    def ensure_child_dir(self, project_id: str, session_id: str, *parts: str) -> Path:
        root = self.ensure_session_dir(project_id, session_id)
        target = root.joinpath(*parts).resolve()
        if not is_within(target, root):
            raise ValueError("Session temporary path escaped session directory")
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _validate_session_id(session_id: str) -> str:
        clean_session_id = (session_id or "").strip()
        if not clean_session_id or not _SESSION_ID_RE.fullmatch(clean_session_id):
            raise ValueError("Invalid session ID for temporary storage")
        return clean_session_id

    @staticmethod
    def _assert_inside_project(path: Path, project_path: Path) -> None:
        if not is_within(path, project_path):
            raise ValueError("Session temporary path escaped project directory")


session_temp_service = SessionTempService()
