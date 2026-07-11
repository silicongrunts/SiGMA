from __future__ import annotations

"""Lightweight notebook file I/O service.

Cell execution is handled by the embedded Jupyter server (see jupyter_service.py).
This module is responsible only for reading, writing, and creating .ipynb files.
"""

import json
from pathlib import Path

from aiofiles import open as aiofiles_open

from ..core.config import settings
from ..core.utils import generate_id, is_within
from ..core.atomic_file import atomic_write_text


class NotebookService:
    def __init__(self, settings_obj=None):
        self.settings = settings_obj or settings

    def _get_notebook_path(self, project_id: str, path: str) -> Path:
        project_path = self.settings.get_project_path(project_id)
        full_path = (project_path / path).resolve()
        if not is_within(full_path, project_path):
            raise PermissionError("Access denied: path outside project")
        return full_path

    def get_project_relative_path(self, project_id: str, path: str) -> str:
        full_path = self._get_notebook_path(project_id, path)
        project_path = self.settings.get_project_path(project_id).resolve()
        return str(full_path.relative_to(project_path))

    async def read(self, project_id: str, path: str) -> dict:
        full_path = self._get_notebook_path(project_id, path)
        if not full_path.exists():
            raise FileNotFoundError(f"Notebook not found: {path}")

        async with aiofiles_open(full_path, "r", encoding="utf-8") as f:
            content = await f.read()

        try:
            notebook = json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid notebook JSON: {e}") from e

        return notebook

    async def write(self, project_id: str, path: str, notebook: dict) -> dict:
        if "cells" not in notebook:
            notebook["cells"] = []
        if "metadata" not in notebook:
            notebook["metadata"] = {}
        if "nbformat" not in notebook:
            notebook["nbformat"] = 4
        if "nbformat_minor" not in notebook:
            notebook["nbformat_minor"] = 5

        full_path = self._get_notebook_path(project_id, path)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Sync atomic write is acceptable here: local FS, single-user, fast
        atomic_write_text(full_path, json.dumps(notebook, indent=1, ensure_ascii=False))

        return {"success": True, "path": path}

    async def create_empty(self, project_id: str, path: str) -> dict:
        """Create a new notebook with one default code cell."""
        notebook = {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "id": generate_id(),
                    "metadata": {},
                    "outputs": [],
                    "source": [],
                }
            ],
            "metadata": {
                "kernelspec": {
                    "display_name": "Python 3",
                    "language": "python",
                    "name": "python3",
                },
                "language_info": {
                    "name": "python",
                    "version": "3.11.0",
                },
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        return await self.write(project_id, path, notebook)


# --- module-level singleton ------------------------------------------------

notebook_service: NotebookService | None = None


def init_notebook_service(settings_obj=None):
    global notebook_service
    notebook_service = NotebookService(settings_obj)
    return notebook_service
