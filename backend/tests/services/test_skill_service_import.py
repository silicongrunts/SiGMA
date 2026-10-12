"""Service-level tests for skill import scanning, selection and preview.

All filesystem work happens under ``tmp_path`` (monkeypatched ``SIGMA_DIR``)
or a fake "repository" directory.  No network or real git clone is involved:
``_cloned_repo`` is stubbed where the full import/preview path is exercised.
"""

import contextlib
from pathlib import Path

import pytest

from app.core import config
from app.core.exceptions import SkillError
from app.services.skill_service import SkillService


def _skill_md(name: str, desc: str) -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\nBody.\n"


@pytest.fixture
def service(tmp_path, monkeypatch):
    """Return a SkillService whose skills dir is an isolated tmp_path."""
    sigma_dir = tmp_path / ".SiGMA"
    monkeypatch.setattr(config, "SIGMA_DIR", sigma_dir)
    return SkillService()


def _make_repo(root: Path, entries: dict[str, str]) -> None:
    """Create fake repository directories, each holding one SKILL.md."""
    for rel, content in entries.items():
        skill_dir = root / rel
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def _stub_clone(service, repo_dir: Path, monkeypatch, fail_if_called=False):
    """Replace ``_cloned_repo`` with a fake that yields *repo_dir*."""

    @contextlib.asynccontextmanager
    async def fake_cloned_repo(url):
        if fail_if_called:
            raise AssertionError("clone must not be attempted")
        yield repo_dir

    monkeypatch.setattr(service, "_cloned_repo", fake_cloned_repo)


# ---------------------------------------------------------------------------
# _scan_dir
# ---------------------------------------------------------------------------


def test_scan_dir_lists_valid_skills(service, tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo, {
        ".": _skill_md("Root Skill", "root desc"),
        "alpha": _skill_md("Alpha", "alpha desc"),
        "nested/beta": _skill_md("Beta", "beta desc"),
    })

    candidates, skipped = service._scan_dir(repo)

    assert {c["path"] for c in candidates} == {".", "alpha", "nested/beta"}
    assert {c["name"] for c in candidates} == {"Root Skill", "Alpha", "Beta"}
    # Repo-root skill derives its store folder name from the frontmatter.
    by_path = {c["path"]: c for c in candidates}
    assert by_path["."]["dir_name"] == "root-skill"
    assert by_path["alpha"]["dir_name"] == "alpha"
    assert skipped == []


def test_scan_dir_skips_invalid_and_hidden(service, tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo, {
        "alpha": _skill_md("Alpha", "alpha desc"),
        "broken": "---\nname: NoDesc\n---\n",
        ".git/fake": _skill_md("Git", "should never appear"),
    })

    candidates, skipped = service._scan_dir(repo)

    assert [c["path"] for c in candidates] == ["alpha"]
    assert [s["path"] for s in skipped] == ["broken"]
    assert "name" in skipped[0]["reason"] or "description" in skipped[0]["reason"]


def test_scan_dir_skips_oversized_skill_md(service, tmp_path):
    from app.services.skill_service import _MAX_FILE_BYTES

    repo = tmp_path / "repo"
    skill_dir = repo / "huge"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Huge\ndescription: x\n---\n" + "x" * (_MAX_FILE_BYTES + 1),
        encoding="utf-8",
    )

    candidates, skipped = service._scan_dir(repo)

    assert candidates == []
    assert skipped == [{"path": "huge", "reason": "SKILL.md too large"}]


# ---------------------------------------------------------------------------
# import / preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_git_imports_all_when_no_paths(service, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo, {
        "alpha": _skill_md("Alpha", "alpha desc"),
        "nested/beta": _skill_md("Beta", "beta desc"),
    })
    _stub_clone(service, repo, monkeypatch)

    result = await service.import_git("https://example.test/repo.git")

    assert {s["name"] for s in result["imported"]} == {"Alpha", "Beta"}
    assert result["skipped"] == []
    ids = {s["id"] for s in result["imported"]}
    assert all(i.startswith(".") for i in ids)  # imported disabled
    names = {s["name"] for s in service.get_all_skills()}
    assert names == {"Alpha", "Beta"}


@pytest.mark.asyncio
async def test_import_git_filters_by_paths(service, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo, {
        "alpha": _skill_md("Alpha", "alpha desc"),
        "beta": _skill_md("Beta", "beta desc"),
    })
    _stub_clone(service, repo, monkeypatch)

    result = await service.import_git(
        "https://example.test/repo.git", paths=["beta", "gone"]
    )

    assert [s["name"] for s in result["imported"]] == ["Beta"]
    assert result["skipped"] == [
        {"path": "gone", "reason": "Not found in repository"}
    ]
    assert [s["name"] for s in service.get_all_skills()] == ["Beta"]


@pytest.mark.asyncio
async def test_import_git_rejects_malformed_paths(service, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo, {"alpha": _skill_md("Alpha", "alpha desc")})
    _stub_clone(service, repo, monkeypatch, fail_if_called=True)

    for bad in (["../evil"], ["/abs"], ["a/.hidden"], ["a//b"], [""]):
        with pytest.raises(SkillError):
            await service.import_git("https://example.test/repo.git", paths=bad)


@pytest.mark.asyncio
async def test_import_git_root_skill_drops_git_dir(service, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo, {".": _skill_md("Root Skill", "root desc")})
    (repo / ".git").mkdir()
    (repo / ".git" / "objects").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    _stub_clone(service, repo, monkeypatch)

    result = await service.import_git("https://example.test/repo.git")

    assert len(result["imported"]) == 1
    target = service._skills_dir / result["imported"][0]["id"]
    assert (target / "SKILL.md").is_file()
    assert not (target / ".git").exists()


@pytest.mark.asyncio
async def test_import_resolves_folder_collision_with_suffix(service, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo, {"alpha": _skill_md("Alpha", "alpha desc")})
    _stub_clone(service, repo, monkeypatch)
    (service._skills_dir / ".alpha").mkdir(parents=True)

    result = await service.import_git("https://example.test/repo.git")

    assert len(result["imported"]) == 1
    new_id = result["imported"][0]["id"]
    assert new_id.startswith(".alpha-")
    assert (service._skills_dir / new_id).is_dir()


@pytest.mark.asyncio
async def test_preview_git_reports_already_exists(service, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _make_repo(repo, {
        "alpha": _skill_md("Alpha", "alpha desc"),
        "beta": _skill_md("Beta", "beta desc"),
        "broken": "---\nname: NoDesc\n---\n",
    })
    _stub_clone(service, repo, monkeypatch)
    (service._skills_dir / ".alpha").mkdir(parents=True)

    data = await service.preview_git("https://example.test/repo.git")

    by_name = {s["name"]: s for s in data["skills"]}
    assert by_name["Alpha"]["already_exists"] is True
    assert by_name["Beta"]["already_exists"] is False
    assert by_name["Alpha"]["path"] == "alpha"
    assert data["ignored_count"] == 1
    # Preview must not touch the store.
    assert service.get_all_skills() == []


@pytest.mark.asyncio
async def test_preview_git_rejects_non_http_url(service):
    with pytest.raises(SkillError):
        await service.preview_git("file:///tmp/some-repo")
    with pytest.raises(SkillError):
        await service.preview_git("https://example.test/x;rm")


# ---------------------------------------------------------------------------
# _validate_selected_paths
# ---------------------------------------------------------------------------


def test_validate_selected_paths_accepts_scan_style_keys():
    SkillService._validate_selected_paths([".", "alpha", "nested/beta", "a/b/c"])


def test_validate_selected_paths_rejects_malformed_keys():
    for bad in (["../evil"], ["/abs"], ["a\\b"], [".hidden"], ["a/."], ["a//b"], [""]):
        with pytest.raises(SkillError):
            SkillService._validate_selected_paths(bad)
