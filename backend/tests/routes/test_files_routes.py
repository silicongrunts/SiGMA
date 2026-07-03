from types import SimpleNamespace

import pytest

from app.models.requests import FileContent, FileExtractRequest
from app.routes import files


@pytest.mark.route
@pytest.mark.asyncio
async def test_update_content_requires_expected_hash(monkeypatch):
    calls = {}

    async def write_file(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return {"path": args[1]}

    fake_service = SimpleNamespace(write_file=write_file)
    monkeypatch.setattr(files, "file_service", fake_service)

    result = await files.update_content(
        "project-1",
        FileContent(path="paper.tex", content="body", force=True, hash="known"),
    )

    assert result["success"] is True
    assert calls["args"] == ("project-1", "paper.tex", "body")
    assert calls["kwargs"] == {
        "force": True,
        "expected_hash": "known",
        "require_expected_hash": True,
    }


@pytest.mark.route
@pytest.mark.asyncio
async def test_extract_archive_returns_conflicts_without_extracting(monkeypatch):
    calls = {"extract": 0}

    async def check_extract_conflicts(project_id, path):
        assert project_id == "project-1"
        assert path == "bundle.zip"
        return ["existing.txt"]

    async def extract_archive(*args, **kwargs):
        calls["extract"] += 1
        return {"ok": True}

    fake_service = SimpleNamespace(
        check_extract_conflicts=check_extract_conflicts,
        extract_archive=extract_archive,
    )
    monkeypatch.setattr(files, "file_service", fake_service)

    result = await files.extract_archive(
        "project-1",
        FileExtractRequest(path="bundle.zip", overwrite=False, skip_conflicts=False),
    )

    assert result["success"] is True
    assert result["data"] == {"conflicts": ["existing.txt"]}
    assert calls["extract"] == 0


@pytest.mark.route
@pytest.mark.asyncio
async def test_extract_archive_skip_conflicts_bypasses_preflight(monkeypatch):
    calls = {"check": 0}

    async def check_extract_conflicts(*args, **kwargs):
        calls["check"] += 1
        return ["existing.txt"]

    async def extract_archive(project_id, path, overwrite=False):
        assert project_id == "project-1"
        assert path == "bundle.zip"
        assert overwrite is False
        return {"extracted": ["new.txt"]}

    fake_service = SimpleNamespace(
        check_extract_conflicts=check_extract_conflicts,
        extract_archive=extract_archive,
    )
    monkeypatch.setattr(files, "file_service", fake_service)

    result = await files.extract_archive(
        "project-1",
        FileExtractRequest(path="bundle.zip", overwrite=False, skip_conflicts=True),
    )

    assert result["success"] is True
    assert result["data"] == {"extracted": ["new.txt"]}
    assert calls["check"] == 0
