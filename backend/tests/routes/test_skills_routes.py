from types import SimpleNamespace

import pytest

from app.core.exceptions import SkillError
from app.models.requests import SkillFileContentRequest, SkillImportGitRequest
from app.routes import skills


class FakeUpload:
    def __init__(self, filename: str | None):
        self.filename = filename


@pytest.mark.route
@pytest.mark.asyncio
async def test_import_skill_zip_rejects_non_zip_filename():
    with pytest.raises(SkillError):
        await skills.import_skill_zip(FakeUpload("skill.txt"))


@pytest.mark.route
@pytest.mark.asyncio
async def test_import_skill_git_passes_url_and_paths(monkeypatch):
    calls = {}

    async def import_git(url, paths=None):
        calls["args"] = (url, paths)
        return {"imported": [], "skipped": []}

    monkeypatch.setattr(skills, "skill_service", SimpleNamespace(import_git=import_git))

    result = await skills.import_skill_git(
        SkillImportGitRequest(url="https://example.test/repo.git", paths=["a", "b"])
    )

    assert result["data"] == {"imported": [], "skipped": []}
    assert calls["args"] == ("https://example.test/repo.git", ["a", "b"])


@pytest.mark.route
@pytest.mark.asyncio
async def test_preview_skill_git_passes_url(monkeypatch):
    calls = {}

    async def preview_git(url):
        calls["url"] = url
        return {
            "skills": [{
                "path": ".",
                "name": "demo",
                "description": "a demo",
                "already_exists": False,
            }],
            "ignored_count": 0,
        }

    monkeypatch.setattr(skills, "skill_service", SimpleNamespace(preview_git=preview_git))

    result = await skills.preview_skill_git(
        SkillImportGitRequest(url="https://example.test/repo.git")
    )

    assert result["data"]["skills"][0]["name"] == "demo"
    assert calls["url"] == "https://example.test/repo.git"


@pytest.mark.route
@pytest.mark.asyncio
async def test_write_skill_file_passes_hash(monkeypatch):
    calls = {}

    def write_file(skill_id, file_path, content, content_hash):
        calls["write"] = (skill_id, file_path, content, content_hash)
        return {"hash": "new"}

    monkeypatch.setattr(skills, "skill_service", SimpleNamespace(write_file=write_file))

    result = await skills.write_skill_file(
        "skill-1",
        SkillFileContentRequest(file_path="SKILL.md", content="body", hash="old"),
    )

    assert result["data"] == {"hash": "new"}
    assert calls["write"] == ("skill-1", "SKILL.md", "body", "old")
