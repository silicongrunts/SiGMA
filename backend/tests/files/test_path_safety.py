"""Path traversal and symlink-escape contracts for the sandbox boundary.

Every sandboxed read/write/edit goes through ``FileService.safe_join``,
which resolves the joined path (following symlinks) and refuses anything
that lands outside the project root. These tests pin that boundary:

* ``../`` traversal is refused with the traversal error, never executed.
* A symlink inside the sandbox pointing at an external target is refused
  before any byte moves — the external file is left untouched.
* A symlink that resolves entirely inside the sandbox keeps working.
"""

import pytest

from app.agents.tools.file_tools import _edit_file, _read_file, _write_file
from app.core.exceptions import FileSystemError
from app.services.file_service import file_service

pytestmark = pytest.mark.security


@pytest.fixture
def outside_dir(tmp_path_factory):
    """A directory outside the sandbox (pytest-managed temp, never the repo)."""
    return tmp_path_factory.mktemp("outside")


# ── parent traversal ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "escape",
    ["../{name}/secret.txt", "sub/../../{name}/secret.txt"],
)
async def test_read_parent_traversal_refused(sandbox, outside_dir, escape):
    (outside_dir / "secret.txt").write_text("external secret")

    with pytest.raises(FileSystemError) as excinfo:
        await file_service.read_file("proj", escape.format(name=outside_dir.name))

    assert excinfo.value.code == "PERMISSION_DENIED"
    assert "Path traversal attempt detected" in str(excinfo.value)
    assert (outside_dir / "secret.txt").read_text() == "external secret"


@pytest.mark.asyncio
async def test_tool_read_traversal_reports_error(sandbox, outside_dir):
    (outside_dir / "secret.txt").write_text("external secret")

    result = await _read_file("proj", "sess", f"../{outside_dir.name}/secret.txt")

    assert result.startswith("Error:")
    assert "Path traversal attempt detected" in result


@pytest.mark.asyncio
async def test_tool_write_traversal_refused(sandbox, outside_dir):
    target = f"../{outside_dir.name}/escape.txt"

    with pytest.raises(FileSystemError) as excinfo:
        await _write_file("proj", "sess", target, "escaped")

    assert "Path traversal attempt detected" in str(excinfo.value)
    assert not (outside_dir / "escape.txt").exists()


@pytest.mark.asyncio
async def test_tool_edit_traversal_refused(sandbox, outside_dir):
    victim = outside_dir / "victim.txt"
    victim.write_text("external content")

    result = await _edit_file(
        "proj", "sess", f"../{outside_dir.name}/victim.txt",
        "external", "hacked",
    )

    assert result.startswith("Error:")
    assert "Path traversal attempt detected" in result
    assert victim.read_text() == "external content"


# ── symlink escapes ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_symlink_escape_read_refused(sandbox, outside_dir):
    (outside_dir / "target.txt").write_text("external")
    (sandbox / "link.txt").symlink_to(outside_dir / "target.txt")

    with pytest.raises(FileSystemError) as excinfo:
        await file_service.read_file("proj", "link.txt")

    assert excinfo.value.code == "PERMISSION_DENIED"
    assert "Path traversal attempt detected" in str(excinfo.value)


@pytest.mark.asyncio
async def test_symlink_escape_write_refused_and_external_untouched(
        sandbox, outside_dir):
    target = outside_dir / "target.txt"
    target.write_text("original")
    (sandbox / "link.txt").symlink_to(target)

    with pytest.raises(FileSystemError):
        await file_service.write_file("proj", "link.txt", "hacked")

    assert target.read_text() == "original"
    assert (sandbox / "link.txt").is_symlink()


@pytest.mark.asyncio
async def test_symlink_to_missing_external_target_refused(sandbox, outside_dir):
    """Even a symlink whose target does not exist yet must be refused —
    resolve() follows the link to the external path before any write."""
    (sandbox / "link.txt").symlink_to(outside_dir / "does_not_exist.txt")

    with pytest.raises(FileSystemError) as excinfo:
        await file_service.write_file("proj", "link.txt", "hacked")

    assert excinfo.value.code == "PERMISSION_DENIED"
    assert not (outside_dir / "does_not_exist.txt").exists()


@pytest.mark.asyncio
async def test_symlinked_directory_escape_refused(sandbox, outside_dir):
    (outside_dir / "secret.txt").write_text("external")
    (sandbox / "docs").symlink_to(outside_dir)

    with pytest.raises(FileSystemError) as excinfo:
        await file_service.read_file("proj", "docs/secret.txt")

    assert excinfo.value.code == "PERMISSION_DENIED"


# ── in-sandbox symlinks are not over-blocked ─────────────────────────


@pytest.mark.asyncio
async def test_symlink_within_sandbox_still_readable_and_writable(sandbox):
    real = sandbox / "real.txt"
    real.write_text("v1")
    (sandbox / "alias.txt").symlink_to(real)

    # Reads follow the in-sandbox symlink.
    assert await file_service.read_file("proj", "alias.txt") == "v1"

    # Writes land on the resolved target; the symlink itself stays intact.
    await file_service.write_file("proj", "alias.txt", "v2")
    assert (sandbox / "alias.txt").is_symlink()
    assert real.read_text() == "v2"
    assert await file_service.read_file("proj", "alias.txt") == "v2"
