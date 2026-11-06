"""Memory ``rm`` tool: containment, index protection, internal visibility."""

import pytest

from app.agents.tools.registry import tool_registry
from app.core.config import settings


@pytest.fixture
def rm_tool():
    from app.agents.tools.memory_file_tools import _rm_memory_file  # noqa: F401  (import registers the tool)

    tool = tool_registry.get("rm")
    assert tool is not None
    return tool


@pytest.fixture
def memory_tree(project):
    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text("- fact.md — fact\n", encoding="utf-8")
    (root / "fact.md").write_text("some fact\n", encoding="utf-8")
    return root


async def test_deletes_fact_file_inside_memory_root(project, rm_tool, memory_tree):
    result = await rm_tool.call(
        path=".SiGMA/memory/fact.md", project_id=project, session_id="s",
    )
    assert "Deleted" in result
    assert not (memory_tree / "fact.md").exists()
    assert (memory_tree / "MEMORY.md").exists()  # index untouched


async def test_refuses_absolute_path_outside_memory_root(project, rm_tool, memory_tree, tmp_path):
    outside = tmp_path / "victim.md"
    outside.write_text("do not touch", encoding="utf-8")
    result = await rm_tool.call(
        path=str(outside), project_id=project, session_id="s",
    )
    assert result.startswith("Error") and "outside" in result
    assert outside.exists()


async def test_refuses_traversal_escape(project, rm_tool, memory_tree):
    result = await rm_tool.call(
        path=".SiGMA/memory/../../../escape.md", project_id=project, session_id="s",
    )
    assert result.startswith("Error")
    assert not (settings.get_project_path(project) / "escape.md").exists()


async def test_refuses_memory_index(project, rm_tool, memory_tree):
    result = await rm_tool.call(
        path=".SiGMA/memory/MEMORY.md", project_id=project, session_id="s",
    )
    assert result.startswith("Error") and "MEMORY.md" in result
    assert (memory_tree / "MEMORY.md").exists()


async def test_refuses_missing_file(project, rm_tool, memory_tree):
    result = await rm_tool.call(
        path=".SiGMA/memory/ghost.md", project_id=project, session_id="s",
    )
    assert result.startswith("Error") and "not found" in result


def test_rm_is_internal_and_hidden_from_default_catalog(rm_tool):
    assert rm_tool.internal is True
    # default role listings never advertise it...
    from app.agents.tool_schema_service import tool_schemas_for_model_role

    default_names = {s["function"]["name"] for s in tool_schemas_for_model_role("supervisor")}
    assert "rm" not in default_names
    # ...but the consolidation allowlist does
    consolidated = {
        s["function"]["name"]
        for s in tool_schemas_for_model_role("supervisor", allowed_tools=frozenset({"rm"}))
    }
    assert "rm" in consolidated


# ---------------------------------------------------------------------------
# 20k per-file size cap (write + edit)
# ---------------------------------------------------------------------------


async def test_write_over_memory_limit_rejected(project, memory_tree, monkeypatch):
    from app.agents.tools.file_tools import _write_file

    monkeypatch.setattr(settings.memory, "max_file_chars", 20)
    target = str(memory_tree / "huge.md")
    result = await _write_file(project, "sess-cap", target, "x" * 25)
    assert result.startswith("Error") and "memory file limit" in result
    assert not (memory_tree / "huge.md").exists()

    # under the limit passes
    result = await _write_file(project, "sess-cap", target, "x" * 10)
    assert "Error" not in result


async def test_edit_over_memory_limit_rejected(project, memory_tree, monkeypatch):
    from app.agents.tools.file_tools import _edit_file
    from app.agents.tools.read_state import record_path_read

    monkeypatch.setattr(settings.memory, "max_file_chars", 8)
    target = memory_tree / "fact.md"
    target.write_text("hello\n", encoding="utf-8")
    record_path_read("sess-edit", target, "hello\n")

    # projected: 6 - 5 + 11 = 12 > 8 → rejected before any write
    result = await _edit_file(
        project, "sess-edit", str(target), "hello", "hello world",
    )
    assert result.startswith("Error") and "memory file limit" in result
    assert target.read_text(encoding="utf-8") == "hello\n"


async def test_edit_limit_counts_crlf_restored_content(project, memory_tree, monkeypatch):
    """The cap is on the file as stored: the authoritative check runs after
    the CRLF restore, so an edit whose normalized result fits but whose
    on-disk CRLF form would exceed the cap is rejected."""
    from app.agents.tools.file_tools import _edit_file
    from app.agents.tools.read_state import record_path_read

    monkeypatch.setattr(settings.memory, "max_file_chars", 30)
    target = memory_tree / "crlf.md"
    # 10 lines of "aa": 30 normalized characters, 40 stored with CRLF endings
    target.write_bytes(b"aa\r\n" * 10)
    record_path_read("sess-crlf", target, "aa\n" * 10)

    # same-length replace_all: projection and normalized checks see 30 <= 30,
    # the stored form would be 40 > 30
    result = await _edit_file(
        project, "sess-crlf", str(target), "aa", "bb", replace_all=True,
    )
    assert result.startswith("Error") and "memory file limit" in result
    assert target.read_bytes() == b"aa\r\n" * 10
