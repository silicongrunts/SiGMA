"""Memory write gate: foreground auto-allow, subagent hard denial.

The memory counters move only when a write actually lands (rejected writes
never count)."""

import pytest

from app.core.config import settings
from app.database.unit_of_work import UnitOfWork
from app.services import permission_executor as pe
from app.services.llm_loop_runner import LLMLoopRunner
from app.services.memory_service import (
    MEMORY_CONSOLIDATE_KEY,
    _read_extract_state,
    memory_service,
)
from app.services.pauses import PermissionRequestPause


@pytest.fixture(autouse=True)
def _fresh_pending_slot():
    memory_service._pending.clear()
    yield
    memory_service._pending.clear()


@pytest.fixture
def memory_tree(project):
    root = settings.get_sigma_path(project) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def _consolidate_value(project):
    async with UnitOfWork(project) as uow:
        return await uow.config.get(MEMORY_CONSOLIDATE_KEY, "")


async def test_foreground_memory_write_gate_allows_without_counting(project, memory_tree):
    denial = await pe._check_write(
        "write", {"file_path": ".SiGMA/memory/new.md", "content": "fact"},
        project, memory_write_mode="allow",
    )
    assert denial is None  # no approval dialog, no category classification
    # counting happens in execute_with_permission once the write has landed
    assert await _consolidate_value(project) == ""


async def test_memory_write_counted_only_when_it_lands(project, memory_tree):
    """A write the tool itself rejects (over the file cap) must not reset or
    deplete the memory counters — only a landed write counts."""
    target = memory_tree / "fact.md"

    async def _write(content):
        return await pe.execute_with_permission(
            "write",
            {
                "file_path": str(target),
                "content": content,
                "project_id": project,
                "session_id": "sess-count",
            },
            project_id=project,
            session_id="sess-count",
            memory_write_mode="allow",
        )

    bounced = await _write("x" * (settings.memory.max_file_chars + 1))
    assert bounced.startswith("Error")
    assert await _consolidate_value(project) == ""

    landed = await _write("fact")
    assert "Error" not in landed
    assert await _consolidate_value(project) == str(settings.memory.consolidation_interval - 1)
    # the writing session's extraction window is the one that resets
    state = await _read_extract_state(project, "sess-count")
    assert state["extract_remaining"] == settings.memory.extraction_interval


async def test_index_write_lands_without_consolidation_depletion(project, memory_tree):
    """A MEMORY.md write resets the writing session's extraction window but
    does not deplete the consolidation counter — index maintenance is
    bookkeeping around a fact write, not new material to reorganize."""
    await memory_service.note_main_loop_llm_call(project, "sess-idx")
    interval = settings.memory.extraction_interval
    assert (await _read_extract_state(project, "sess-idx"))["extract_remaining"] == interval - 1

    result = await pe.execute_with_permission(
        "write",
        {
            "file_path": str(memory_tree / "MEMORY.md"),
            "content": "- [A](a.md) — one fact",
            "project_id": project,
            "session_id": "sess-idx",
        },
        project_id=project,
        session_id="sess-idx",
        memory_write_mode="allow",
    )
    assert "Error" not in result
    assert await _consolidate_value(project) == ""
    assert (await _read_extract_state(project, "sess-idx"))["extract_remaining"] == interval


async def test_subagent_memory_write_denied(project, memory_tree):
    denial = await pe._check_write(
        "write", {"file_path": ".SiGMA/memory/new.md", "content": "fact"},
        project, memory_write_mode="deny",
    )
    assert denial is not None
    assert ".SiGMA/memory" in denial and "Subagents" in denial
    # the denial must not touch the foreground counters
    assert await _consolidate_value(project) == ""


async def test_non_memory_write_still_needs_approval(project, memory_tree):
    # memory_write_mode only affects memory-root targets; an ordinary sandbox
    # write keeps its normal approval flow (no auto-approve configured here)
    with pytest.raises(PermissionRequestPause):
        await pe._check_write(
            "write", {"file_path": "notes.md", "content": "x"},
            project, memory_write_mode="allow",
        )


async def test_execute_with_permission_end_to_end_write(project, memory_tree):
    # the LLM runner injects project_id/session_id into tool_args before the
    # permission gate runs; mirror that contract here. The target is absolute
    # because write's body resolves relative paths via the project registry,
    # which the shared ``project`` fixture does not rebind.
    target = str(memory_tree / "echo.md")
    result = await pe.execute_with_permission(
        "write",
        {
            "file_path": target,
            "content": "hello memory",
            "project_id": project,
            "session_id": "sess-e2e",
        },
        project_id=project,
        memory_write_mode="allow",
    )
    assert "Error" not in result
    assert (memory_tree / "echo.md").read_text(encoding="utf-8") == "hello memory"


async def test_internal_rm_refused_even_in_allow_mode(project, memory_tree):
    # rm belongs to the background consolidation loop alone: the permission
    # gate refuses it for every foreground/subagent caller, allow or not
    target = memory_tree / "gone.md"
    target.write_text("fact", encoding="utf-8")
    result = await pe.execute_with_permission(
        "rm",
        {"path": str(target), "project_id": project, "session_id": "sess-rm"},
        project_id=project,
        memory_write_mode="allow",
    )
    assert "internal" in result
    assert target.exists()


async def test_internal_rm_refused_by_default_tool_dispatch():
    # annotation-style loops dispatch via execute_tool_default, which
    # bypasses the permission gate — the refusal must hold there too
    result = await LLMLoopRunner.execute_tool_default(
        "rm", {"path": ".SiGMA/memory/x.md"},
    )
    assert "internal" in result
