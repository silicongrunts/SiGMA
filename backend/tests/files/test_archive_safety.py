"""FileService archive extraction and file-tree path safety.

Covers the real archive pipeline (``sanitize_member`` ->
``_validate_extract_dest`` -> ``extract_archive``) so malicious zip members
never escape the extraction target, plus the project file-tree robustness
guarantees (symlink loops are not followed, hidden paths are rejected).

Note: the file-tree section lives here because it guards the same
FileService path-containment contract and ``tests/files/`` is its domain
directory per RULES/TESTING.md.
"""

import os
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from app.services.file_service import FileService


pytestmark = pytest.mark.security


def _service_for_project(tmp_path, project_id: str = "p1") -> FileService:
    service = FileService.__new__(FileService)
    project_path = tmp_path / project_id
    project_path.mkdir()
    service.get_project_path = lambda pid: project_path

    async def _noop_snapshot(pid, paths=None):
        return None

    service._after_file_mutation = _noop_snapshot
    return service


# ---------------------------------------------------------------------------
# File tree robustness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_tree_does_not_follow_directory_symlink_loop(tmp_path):
    """A symlink pointing back to an ancestor must not recurse forever."""
    service = _service_for_project(tmp_path)
    root = service.get_project_path("p1")
    (root / "folder").mkdir()
    try:
        os.symlink(root, root / "folder" / "loop")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    tree = await service.get_project_tree("p1")
    folder = tree["root"]["children"][0]
    loop = folder["children"][0]

    assert loop["name"] == "loop"
    assert loop["type"] == "file"
    assert loop["symlink"] is True
    assert loop["children"] == []


@pytest.mark.asyncio
async def test_file_tree_create_rejects_hidden_paths(tmp_path):
    service = _service_for_project(tmp_path)

    with pytest.raises(Exception) as exc_info:
        await service.create_item("p1", ".env", is_dir=False)

    assert getattr(exc_info.value, "code", "") == "INVALID_PATH"


@pytest.mark.asyncio
async def test_file_tree_rename_rejects_hidden_name(tmp_path):
    service = _service_for_project(tmp_path)
    await service.create_item("p1", "visible.txt", is_dir=False)

    with pytest.raises(Exception) as exc_info:
        await service.rename_item("p1", "visible.txt", ".hidden")

    assert getattr(exc_info.value, "code", "") == "INVALID_PATH"


# ---------------------------------------------------------------------------
# sanitize_member unit tests
# ---------------------------------------------------------------------------

class TestSanitizeMember:
    """Verify that archive member names are validated before extraction."""

    def test_sanitize_rejects_absolute_path(self):
        """/etc/passwd is rejected entirely (returns empty string)."""
        result = FileService.sanitize_member("/etc/passwd")
        assert result == ""

    def test_sanitize_rejects_parent_traversal(self):
        """../../../etc/shadow is rejected entirely (returns empty string)."""
        result = FileService.sanitize_member("../../../etc/shadow")
        assert result == ""

    def test_sanitize_rejects_windows_drive(self):
        """C:\\Windows\\System32 is rejected entirely."""
        result = FileService.sanitize_member("C:\\Windows\\System32")
        assert result == ""

    def test_sanitize_allows_normal_relative(self):
        """Normal relative paths pass through unchanged."""
        result = FileService.sanitize_member("src/main.py")
        assert result == "src/main.py"

    def test_sanitize_passes_dot_slash(self):
        """'./README.md' passes through — Path normalizes the '.' away."""
        result = FileService.sanitize_member("./README.md")
        # Path("./README.md").parts == ('README.md',), so it passes as-is
        assert result == "./README.md"

    def test_sanitize_returns_empty_for_root_only(self):
        """'/' alone produces empty (nothing to extract)."""
        result = FileService.sanitize_member("/")
        assert result == ""


# ---------------------------------------------------------------------------
# _validate_extract_dest unit tests
# ---------------------------------------------------------------------------

class TestValidateExtractDest:
    """Verify the double-containment check that backs symlink safety."""

    def test_validate_allows_normal_member(self, tmp_path):
        """Normal member inside target_dir and project_root passes validation."""
        svc = FileService.__new__(FileService)
        project_root = tmp_path
        target_dir = project_root / "extract"
        target_dir.mkdir()
        dest = target_dir / "src" / "main.py"
        assert svc._validate_extract_dest(dest, target_dir, project_root) is True

    def test_validate_rejects_absolute_escape(self, tmp_path):
        """Dest resolving to /etc/passwd is rejected."""
        svc = FileService.__new__(FileService)
        project_root = tmp_path
        target_dir = project_root / "extract"
        target_dir.mkdir()
        dest = Path("/etc/passwd")
        assert svc._validate_extract_dest(dest, target_dir, project_root) is False

    def test_validate_rejects_symlink_escape(self, tmp_path):
        """If target_dir is a symlink to outside project_root, dest is rejected."""
        svc = FileService.__new__(FileService)
        project_root = tmp_path / "project"
        project_root.mkdir()
        # tmp_path itself resolves outside project_root.
        symlink_dir = project_root / "symlink_target"
        symlink_dir.symlink_to(tmp_path)
        dest = symlink_dir / "file.txt"
        assert svc._validate_extract_dest(dest, symlink_dir, project_root) is False


# ---------------------------------------------------------------------------
# Real end-to-end archive extraction
# ---------------------------------------------------------------------------

class TestExtractArchiveEndToEnd:
    """Drive the real FileService.extract_archive against malicious archives."""

    @pytest.mark.asyncio
    async def test_extract_archive_skips_malicious_members(
        self, tmp_path, tmp_path_factory,
    ):
        """A zip containing parent-traversal, absolute, and drive-letter
        members must not write outside the project; legal members land in
        the extraction folder and the staging directory is cleaned up."""
        service = _service_for_project(tmp_path)
        project_root = service.get_project_path("p1")

        # Escape canary for the absolute-path member: a dedicated directory
        # under pytest's own tmp root, outside the project sandbox. Unlike a
        # fixed global path (/etc) it never depends on pre-existing machine
        # state, and pytest prunes it with the per-run tmp tree.
        canary_dir = tmp_path_factory.mktemp("archive_escape_canary")
        canary = canary_dir / "evil_absolute.txt"

        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("safe.txt", "safe content")
            zf.writestr("nested/inner.txt", "inner")
            zf.writestr("../evil_traversal.txt", "escape attempt")
            zf.writestr(str(canary), "absolute escape")
            zf.writestr("C:\\evil_drive.txt", "windows drive escape")
        archive_path = project_root / "bundle.zip"
        archive_path.write_bytes(buf.getvalue())

        result = await service.extract_archive("p1", "bundle.zip")

        assert result["extracted_to"] == "bundle"
        assert result["file_count"] == 2
        target_dir = project_root / "bundle"
        assert (target_dir / "safe.txt").read_text() == "safe content"
        assert (target_dir / "nested" / "inner.txt").read_text() == "inner"

        # No malicious member escaped: nothing anywhere under the temp
        # userdata root, and nothing at the absolute member's destination —
        # a file there would mean the extractor honored the absolute path.
        escaped = [p for p in tmp_path.rglob("*") if "evil" in p.name]
        assert escaped == []
        assert not canary.exists()

        # The atomic staging directory leaves no residue behind.
        assert list(project_root.glob(".extracting-*")) == []
