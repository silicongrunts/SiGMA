from types import SimpleNamespace

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.models.requests import ProjectConfigUpdate
from app.services.git_service import (
    DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB,
    parse_max_new_file_mb,
)
from app.services.project_service import ProjectService


class FakeConfigRepo:
    def __init__(self, values):
        self.values = values

    async def get_all(self):
        return dict(self.values)

    async def set(self, key, value):
        self.values[key] = value


class FakeUnitOfWork:
    def __init__(self, values):
        self.config = FakeConfigRepo(values)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_project_snapshot_limit_defaults_and_reads_per_project(
        monkeypatch):
    service = ProjectService.__new__(ProjectService)
    values = {}
    monkeypatch.setattr(
        "app.database.unit_of_work.UnitOfWork",
        lambda project_id: FakeUnitOfWork(values),
    )

    config = await service.get_project_config("project-a")
    assert config["snapshot_max_new_file_mb"] == DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB

    values["snapshot_max_new_file_mb"] = "20"
    config = await service.get_project_config("project-a")
    assert config["snapshot_max_new_file_mb"] == 20


async def test_project_snapshot_limit_update_is_persisted(monkeypatch):
    service = ProjectService.__new__(ProjectService)
    values = {}
    monkeypatch.setattr(
        "app.database.unit_of_work.UnitOfWork",
        lambda project_id: FakeUnitOfWork(values),
    )

    await service.update_project_config(
        "project-a", SimpleNamespace(
            snapshot_enabled=None,
            snapshot_interval_minutes=None,
            snapshot_max_new_file_mb=20,
            tips=None,
        ),
    )

    assert values["snapshot_max_new_file_mb"] == "20"


def test_project_snapshot_limit_rejects_non_positive_values():
    with pytest.raises(PydanticValidationError):
        ProjectConfigUpdate(snapshot_max_new_file_mb=0)


def test_parse_max_new_file_mb_defaults_on_invalid_values():
    assert parse_max_new_file_mb("20") == 20
    assert parse_max_new_file_mb("1") == 1
    assert parse_max_new_file_mb("0") == DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB
    assert parse_max_new_file_mb("abc") == DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB
    assert parse_max_new_file_mb(None) == DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB
