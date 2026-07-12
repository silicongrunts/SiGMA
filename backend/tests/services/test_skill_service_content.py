"""Service-level tests for ``SkillService.get_skill_content``.

Covers the hint that steers the LLM toward ``skill_load`` (rather than the
``read`` tool) when a skill bundles reference files.  All writes go under
``tmp_path`` via a monkeypatched ``SIGMA_DIR``; no real userdata is touched.
"""

from pathlib import Path

import pytest

from app.core import config
from app.services.skill_service import SkillService

_SKILL_MD = """\
---
name: demo
description: a demo skill
---

# Demo skill

Body text.
"""

_HINT_MARKER = "do NOT use the `read` tool"


def _make_skill(sigma_dir: Path, skill_id: str, *, files: dict[str, str]) -> None:
    """Create a skill directory under *sigma_dir* with the given files."""
    skill_dir = sigma_dir / "skill" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def service(tmp_path, monkeypatch):
    """Return a SkillService whose skills dir is an isolated tmp_path."""
    sigma_dir = tmp_path / ".SiGMA"
    monkeypatch.setattr(config, "SIGMA_DIR", sigma_dir)
    # SkillService reads settings.SIGMA_DIR at construction time.
    return SkillService()


def test_hint_appended_when_skill_has_bundled_files(service, tmp_path):
    _make_skill(
        tmp_path / ".SiGMA",
        "with-refs",
        files={"SKILL.md": _SKILL_MD, "references/foo.md": "# Foo\n"},
    )
    content = service.get_skill_content("with-refs")
    assert "skill_load" in content
    assert _HINT_MARKER in content
    assert content.rstrip().endswith("access skill files.")


def test_no_hint_for_single_file_skill(service, tmp_path):
    _make_skill(
        tmp_path / ".SiGMA",
        "solo",
        files={"SKILL.md": _SKILL_MD},
    )
    content = service.get_skill_content("solo")
    assert _HINT_MARKER not in content
    assert content.strip() == _SKILL_MD.strip()


def test_no_hint_when_reading_subfile(service, tmp_path):
    _make_skill(
        tmp_path / ".SiGMA",
        "multi",
        files={"SKILL.md": _SKILL_MD, "references/foo.md": "# Foo\n"},
    )
    content = service.get_skill_content("multi", "references/foo.md")
    assert _HINT_MARKER not in content
    assert content.strip() == "# Foo"


def test_no_hint_when_only_hidden_sibling_exists(service, tmp_path):
    _make_skill(
        tmp_path / ".SiGMA",
        "hidden-only",
        files={"SKILL.md": _SKILL_MD, ".secret.md": "# secret\n"},
    )
    content = service.get_skill_content("hidden-only")
    assert _HINT_MARKER not in content
