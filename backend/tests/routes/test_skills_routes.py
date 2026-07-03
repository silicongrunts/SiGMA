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
async def test_import_skill_git_passes_url(monkeypatch):
    calls = {}

    async def import_git(url):
        calls["url"] = url
        return {"imported": 1}

    monkeypatch.setattr(skills, "skill_service", SimpleNamespace(import_git=import_git))

    result = await skills.import_skill_git(SkillImportGitRequest(url="https://example.test/repo.git"))

    assert result["data"] == {"imported": 1}
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
