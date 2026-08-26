"""
File listings expose display metadata: sizes and modification times.

The Synthesis file tree's hover tooltip shows a file's size plus its
modification time, and only a folder's modification time. Both the lazy
`/children` listing and the full `/tree` must therefore carry `size` and
`mtime` on file nodes, and `mtime` (never `size`) on directory nodes.
"""
import pytest

from app.services.file_service import file_service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_children_includes_size_and_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(file_service, "get_project_path", lambda project_id: tmp_path)

    (tmp_path / "big.bin").write_bytes(b"\x00" * 4096)
    (tmp_path / "sub").mkdir()

    result = await file_service.get_children("project", "")

    by_name = {child["name"]: child for child in result["children"]}
    assert by_name["big.bin"]["size"] == 4096
    assert isinstance(by_name["big.bin"]["mtime"], float)
    assert "size" not in by_name["sub"]  # directories have no size
    assert isinstance(by_name["sub"]["mtime"], float)
    assert by_name["big.bin"]["type"] == "file"
    assert by_name["sub"]["type"] == "directory"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_children_omits_metadata_for_vanished_entries(tmp_path, monkeypatch):
    """A dangling symlink still lists (its type falls back to file) but carries
    no mtime/size — the listing must not fail on entries unreadable mid-scan."""
    monkeypatch.setattr(file_service, "get_project_path", lambda project_id: tmp_path)

    try:
        (tmp_path / "dangling").symlink_to(tmp_path / "missing-target")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    result = await file_service.get_children("project", "")

    by_name = {child["name"]: child for child in result["children"]}
    assert by_name["dangling"]["type"] == "file"
    assert "mtime" not in by_name["dangling"]
    assert "size" not in by_name["dangling"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_project_tree_includes_size_and_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(file_service, "get_project_path", lambda project_id: tmp_path)

    (tmp_path / "data.csv").write_text("a,b\n1,2\n")  # 8 bytes

    result = await file_service.get_project_tree("project")

    root = result["root"]
    file_node = next(c for c in root["children"] if c["name"] == "data.csv")
    assert file_node["size"] == 8
    assert isinstance(file_node["mtime"], float)
    assert "size" not in root  # root is a directory
    assert isinstance(root["mtime"], float)
