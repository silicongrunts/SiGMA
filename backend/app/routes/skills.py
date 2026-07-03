"""
Skill routes — list, toggle, and manage files within global skills.

Skills are stored under ``userdata/.SiGMA/skill/`` as folders containing
``SKILL.md`` files.  This router provides endpoints for listing/toggling
skills and full file CRUD within each skill directory.
"""

from fastapi import APIRouter, Query, UploadFile, File

from app.models.requests import (
    SkillFileContentRequest,
    SkillFileCreateRequest,
    SkillFileRenameRequest,
    SkillImportGitRequest,
)
from app.services.skill_service import skill_service
from app.core.response import ok

router = APIRouter(prefix="/skills", tags=["skills"])


# ---------------------------------------------------------------------------
# Skill-level operations
# ---------------------------------------------------------------------------

@router.get("")
async def list_skills():
    """Return all skills with their id, name, description and enabled state."""
    data = skill_service.get_all_skills()
    return ok(data)


@router.patch("/{skill_id}/toggle")
async def toggle_skill(skill_id: str):
    """Toggle a skill between enabled and disabled."""
    data = skill_service.toggle_skill(skill_id)
    return ok(data)


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str):
    """Delete a skill directory entirely."""
    data = skill_service.delete_skill(skill_id)
    return ok(data)


# ---------------------------------------------------------------------------
# Skill import — ZIP upload & Git clone
# ---------------------------------------------------------------------------

@router.post("/import/zip")
async def import_skill_zip(file: UploadFile = File(...)):
    """Import skills from an uploaded ZIP archive.

    The ZIP is scanned for directories containing a valid ``SKILL.md``.
    Matching directories are copied to the skill store in disabled state.
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        from app.core.exceptions import SkillError
        raise SkillError("Only .zip files are accepted")
    data = await skill_service.import_zip(file)
    return ok(data)


@router.post("/import/git")
async def import_skill_git(body: SkillImportGitRequest):
    """Import skills from a Git repository.

    The repository is shallow-cloned, scanned for directories containing
    a valid ``SKILL.md``, and matching directories are imported in
    disabled state.
    """
    data = await skill_service.import_git(body.url)
    return ok(data)


# ---------------------------------------------------------------------------
# File-level operations within a skill
# ---------------------------------------------------------------------------

@router.get("/{skill_id}/files")
async def list_skill_files(skill_id: str):
    """List all files and directories inside a skill."""
    data = skill_service.list_files(skill_id)
    return ok(data)


@router.get("/{skill_id}/files/content")
async def read_skill_file(
    skill_id: str,
    file_path: str = Query(..., min_length=1),
):
    """Read the content of a file inside a skill directory."""
    data = skill_service.read_file(skill_id, file_path)
    return ok(data)


@router.put("/{skill_id}/files/content")
async def write_skill_file(skill_id: str, body: SkillFileContentRequest):
    """Write content to a file inside a skill directory.

    SKILL.md is validated for valid YAML frontmatter with non-empty
    ``name`` and ``description`` before the write is accepted.
    """
    data = skill_service.write_file(
        skill_id, body.file_path, body.content, body.hash,
    )
    return ok(data)


@router.post("/{skill_id}/files/create")
async def create_skill_file(skill_id: str, body: SkillFileCreateRequest):
    """Create a new file or directory inside a skill directory."""
    data = skill_service.create_file(skill_id, body.path, body.type)
    return ok(data)


@router.post("/{skill_id}/files/rename")
async def rename_skill_file(skill_id: str, body: SkillFileRenameRequest):
    """Rename a file or directory inside a skill directory.

    Renaming SKILL.md is forbidden.
    """
    data = skill_service.rename_file(skill_id, body.path, body.new_name)
    return ok(data)


@router.delete("/{skill_id}/files")
async def delete_skill_file(
    skill_id: str,
    file_path: str = Query(..., min_length=1),
):
    """Delete a file or empty directory inside a skill directory.

    Deleting SKILL.md is forbidden.
    """
    data = skill_service.delete_file(skill_id, file_path)
    return ok(data)
