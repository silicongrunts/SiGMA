"""
File listings expose file sizes.

The Synthesis preview's binary-file download button shows the size of the
file it offers, so both the lazy `/children` listing and the full `/tree`
must carry a `size` field on file nodes (directories never do).
"""
import pytest

from app.services.file_service import file_service


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_children_includes_file_size(tmp_path, monkeypatch):
    monkeypatch.setattr(file_service, "get_project_path", lambda project_id: tmp_path)

    (tmp_path / "big.bin").write_bytes(b"\x00" * 4096)
    (tmp_path / "sub").mkdir()

    result = await file_service.get_children("project", "")

    by_name = {child["name"]: child for child in result["children"]}
    assert by_name["big.bin"]["size"] == 4096
    assert "size" not in by_name["sub"]  # directories have no size
    assert by_name["big.bin"]["type"] == "file"
    assert by_name["sub"]["type"] == "directory"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_project_tree_includes_file_size(tmp_path, monkeypatch):
    monkeypatch.setattr(file_service, "get_project_path", lambda project_id: tmp_path)

    (tmp_path / "data.csv").write_text("a,b\n1,2\n")  # 8 bytes

    result = await file_service.get_project_tree("project")

    root = result["root"]
    file_node = next(c for c in root["children"] if c["name"] == "data.csv")
    assert file_node["size"] == 8
    assert "size" not in root  # root is a directory
