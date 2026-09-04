"""
Unit tests for the shared permission executor.

Covers the four-category permission model:
- ``file_external`` / ``file_internal``: filesystem writes classified by path
- ``bash``: non-read-only shell commands
- ``notebook``: executing a notebook cell

Auto-approve is read live from the project DB. When a category's flag is on the
executor silently approves (no pause). Otherwise it raises
``PermissionRequestPause`` — the caller (LLM loop runner) catches it and parks
the task as ``awaiting_input``. Read-only tools and the exempt tool families
(library/draw/task/annotation) bypass approval entirely.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
import hashlib
import json

import pytest

from app.agents.tools.file_tools import _edit_file, _write_file
from app.agents.tools.notebook_tools import _notebook_run_cell
from app.agents.tools.read_state import read_state_cache
from app.core.config import settings
from app.core.text_diff import CONTENT_SOFT_LIMIT
from app.services import permission_executor
from app.services.file_service import PathAccessLevel
from app.services.permission_executor import (
    PermissionRequestPause,
    approved_notebook_drift_error,
    restore_approved_read_state,
)


# ── helpers ─────────────────────────────────────────────────────────

def _make_tool_def(*, is_read_only=True, has_call=True, preflight=None):
    tool_def = MagicMock()
    tool_def.is_read_only = is_read_only
    tool_def.call = MagicMock() if has_call else None
    # Explicitly None unless the test opts in — MagicMock would otherwise expose
    # a truthy `preflight` and short-circuit the preflight check incorrectly.
    tool_def.preflight = preflight
    tool_def.input_schema = {}
    return tool_def


class _BashResult:
    """Stand-in for BashPermissionResult with the fields _check_bash reads."""
    def __init__(self, approved: bool, reason: str = "", *, path: str = "rm",
                 operation: str = "execute", content: str = ""):
        self.approved = approved
        self.reason = reason
        self.path = path
        self.operation = operation
        self.content = content


def _auto_approve_patch(project_id: str, mapping: dict[str, bool]):
    """Patch ``_is_auto_approved`` to return values from ``mapping`` by category."""
    async def fake(project_id_arg, category):
        assert project_id_arg == project_id
        return mapping.get(category, False)
    return patch.object(permission_executor, "_is_auto_approved", new=fake)


# ── unknown / callable-less tools ───────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool_returns_error_string():
    with patch.object(permission_executor.tool_registry, "get", return_value=None):
        result = await permission_executor.execute_with_permission(
            "nope", {}, None, project_id="p",
        )
    assert "Unknown tool" in result


@pytest.mark.asyncio
async def test_tool_without_call_returns_error_string():
    tool_def = _make_tool_def(has_call=False)
    result = await permission_executor.execute_with_permission(
        "ghost", {}, tool_def, project_id="p",
    )
    assert "no call implementation" in result


# ── read-only tools bypass approval ─────────────────────────────────

@pytest.mark.asyncio
async def test_readonly_tool_skips_permission():
    tool_def = _make_tool_def(is_read_only=True)
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="data")) as mock_call:
        result = await permission_executor.execute_with_permission(
            "read", {"file_path": "/etc/passwd"}, tool_def,
            project_id="p",
        )
    assert result == "data"
    mock_call.assert_awaited_once()


# ── bash ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_check_approval_proceeds_without_pause():
    """When the bash permission check approves (the read-only allowlist
    path), the executor proceeds directly — no pause and no consultation of
    the bash auto-approve flag, which only governs non-read-only commands."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(True)) as mock_check, \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="file.txt")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "ls", "description": "list files"},
            tool_def, project_id="p",
        )
    assert result == "file.txt"
    mock_check.assert_called_once_with("ls")


@pytest.mark.asyncio
async def test_bash_non_readonly_auto_approve_silently_passes():
    """When bash auto-approve is on, no pause is raised."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(False, "needs approval")), \
         _auto_approve_patch("p", {"bash": True}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="done")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "rm /tmp/x", "description": "cleanup"},
            tool_def, project_id="p",
        )
    assert result == "done"


@pytest.mark.asyncio
async def test_bash_non_readonly_raises_permission_pause():
    """When bash auto-approve is off, PermissionRequestPause is raised."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(False, "needs approval")), \
         _auto_approve_patch("p", {"bash": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "bash", {"command": "rm /tmp/x", "description": "cleanup"},
                tool_def, project_id="p",
            )
    assert exc_info.value.tool == "bash"
    assert exc_info.value.tool_name == "bash"
    assert exc_info.value.description == "cleanup"


@pytest.mark.asyncio
async def test_bash_pause_carries_full_command_within_soft_limit():
    """The approval dialog shows the whole command (within the payload soft
    limit): what the user reviews is what gets executed."""
    tool_def = _make_tool_def(is_read_only=False)
    long_command = "python script.py " + " ".join(f"--opt{i}" for i in range(200))
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(False, "needs approval")), \
         _auto_approve_patch("p", {"bash": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "bash", {"command": long_command}, tool_def, project_id="p",
            )
    assert exc_info.value.content == long_command
    assert exc_info.value.content_truncated is False


@pytest.mark.asyncio
async def test_bash_empty_command_skips_check():
    """Empty command path is a degenerate case — no check, proceed to call."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor, "check_bash_permission") as mock_check, \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="noop")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": ""}, tool_def,
            project_id="p",
        )
    assert result == "noop"
    mock_check.assert_not_called()


# ── notebook_run_cell ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notebook_run_reads_cell_source_as_content():
    tool_def = _make_tool_def(is_read_only=False)
    fake_notebook = {"cells": [{"id": "c1", "cell_type": "code", "source": ["print('hi')\n"]}]}
    fake_location = MagicMock()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(fake_notebook, fake_location))), \
         patch("app.agents.tools.notebook_utils.find_cell_index",
               return_value=0), \
         _auto_approve_patch("p", {"notebook": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "notebook_run_cell",
                {"notebook_path": "nb.ipynb", "cell_id": "c1"},
                tool_def, project_id="p",
            )
    assert exc_info.value.tool == "notebook"
    assert "print('hi')" in exc_info.value.content


@pytest.mark.asyncio
async def test_notebook_run_pause_carries_full_cell_source():
    """Cell source shown for approval must be complete (within the payload
    soft limit), not a preview cut — the user approves exactly the code
    that will run."""
    tool_def = _make_tool_def(is_read_only=False)
    long_source = "x = 1\n" + "y = 2\n" * 500
    fake_notebook = {"cells": [{"id": "c1", "cell_type": "code",
                                "source": [long_source]}]}
    fake_location = MagicMock()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(fake_notebook, fake_location))), \
         patch("app.agents.tools.notebook_utils.find_cell_index",
               return_value=0), \
         _auto_approve_patch("p", {"notebook": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "notebook_run_cell",
                {"notebook_path": "nb.ipynb", "cell_id": "c1"},
                tool_def, project_id="p",
            )
    assert exc_info.value.content == long_source
    assert exc_info.value.content_truncated is False


@pytest.mark.asyncio
async def test_notebook_run_auto_approve_skips_pause():
    tool_def = _make_tool_def(is_read_only=False)
    fake_notebook = {"cells": [{"id": "c1", "cell_type": "code", "source": "print('hi')\n"}]}
    fake_location = MagicMock()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(fake_notebook, fake_location))), \
         patch("app.agents.tools.notebook_utils.find_cell_index",
               return_value=0), \
         _auto_approve_patch("p", {"notebook": True}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="executed")):
        result = await permission_executor.execute_with_permission(
            "notebook_run_cell",
            {"notebook_path": "nb.ipynb", "cell_id": "c1"},
            tool_def, project_id="p",
        )
    assert result == "executed"


# ── file_external / file_internal writes ────────────────────────────

@pytest.mark.asyncio
async def test_write_inside_sandbox_auto_approve_off_raises_pause():
    """Internal writes need approval unless auto-approve is on."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.SANDBOX), \
         _auto_approve_patch("p", {"file_internal": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "write", {"file_path": "/p/file.txt", "content": "x"},
                tool_def, project_id="p",
            )
    assert exc_info.value.tool == "file_internal"


@pytest.mark.asyncio
async def test_write_inside_sandbox_auto_approve_on_silent():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.SANDBOX), \
         _auto_approve_patch("p", {"file_internal": True}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="written")):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/p/file.txt", "content": "x"},
            tool_def, project_id="p",
        )
    assert result == "written"


@pytest.mark.asyncio
async def test_write_outside_sandbox_raises_pause_with_external_category():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         _auto_approve_patch("p", {"file_external": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "write", {"file_path": "/home/x.txt", "content": "x"},
                tool_def, project_id="p",
            )
    assert exc_info.value.tool == "file_external"
    assert exc_info.value.path == "/home/x.txt"


@pytest.mark.asyncio
async def test_write_pause_carries_full_content_within_soft_limit():
    """Write content shown for approval must be complete (within the payload
    soft limit): the user approves exactly the bytes that will land on disk."""
    tool_def = _make_tool_def(is_read_only=False)
    long_content = "# report\n" + "data line\n" * 500
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         _auto_approve_patch("p", {"file_external": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "write", {"file_path": "/home/x.txt", "content": long_content},
                tool_def, project_id="p",
            )
    assert exc_info.value.content == long_content
    assert exc_info.value.content_truncated is False


@pytest.mark.asyncio
async def test_write_pause_truncates_oversized_content():
    """Content beyond CONTENT_SOFT_LIMIT is truncated with the flag set: the
    pause frame is buffered per subscriber and persisted in the interaction
    checkpoint, so one huge write approval must not park a multi-megabyte
    frame in all of them."""
    tool_def = _make_tool_def(is_read_only=False)
    huge_content = "x" * (CONTENT_SOFT_LIMIT + 1000)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         _auto_approve_patch("p", {"file_external": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "write", {"file_path": "/home/x.txt", "content": huge_content},
                tool_def, project_id="p",
            )
    assert exc_info.value.content == "x" * CONTENT_SOFT_LIMIT
    assert exc_info.value.content_truncated is True


@pytest.mark.asyncio
async def test_edit_composes_old_new_diff_as_content():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         _auto_approve_patch("p", {"file_external": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "edit",
                {"file_path": "/p/f", "old_string": "a", "new_string": "b"},
                tool_def, project_id="p",
            )
    assert "--- before ---" in exc_info.value.content
    assert "--- after ---" in exc_info.value.content


@pytest.mark.asyncio
async def test_tmp_treated_as_internal_category():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.TMP), \
         _auto_approve_patch("p", {"file_internal": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "write", {"file_path": "/tmp/x"}, tool_def,
                project_id="p",
            )
    assert exc_info.value.tool == "file_internal"


# ── resume-side target drift / unresolvable snapshot ────────────────

def test_drift_error_when_target_moved_since_approval():
    with patch.object(permission_executor.file_service, "resolve_write_target",
                      return_value=Path("/elsewhere/target.md")):
        err = permission_executor.approved_target_drift_error(
            "p", "/original/target.md", {"file_path": "/original/target.md"},
            category="file_internal",
        )
    assert "changed since approval" in err
    assert "not executed" in err


def test_drift_error_passes_when_target_unchanged():
    with patch.object(permission_executor.file_service, "resolve_write_target",
                      return_value=Path("/original/target.md")):
        err = permission_executor.approved_target_drift_error(
            "p", "/original/target.md", {"file_path": "/original/target.md"},
            category="file_internal",
        )
    assert err == ""


def test_drift_error_refuses_empty_snapshot_for_file_categories():
    """A file-category approval whose snapshot is empty (the target could
    not be resolved at approval time) must not execute blind: the resume
    refuses and the operation is re-requested."""
    for category in ("file_external", "file_internal"):
        err = permission_executor.approved_target_drift_error(
            "p", "", {"file_path": "/p/file.md"}, category=category,
        )
        assert "could not be re-resolved" in err
        assert "not executed" in err


def test_drift_error_allows_empty_snapshot_without_file_target():
    """bash / notebook approvals carry no resolved-target snapshot; an empty
    snapshot there is the normal case and passes the check."""
    for category in ("bash", "notebook", ""):
        err = permission_executor.approved_target_drift_error(
            "p", "", {}, category=category,
        )
        assert err == ""


# ── annotation / library / draw / task tools are exempt ─────────────

@pytest.mark.asyncio
async def test_annotation_tool_bypasses_write_approval():
    """Exempt tools never trigger approval even when ``is_read_only=False``."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="done")) as mock_call:
        result = await permission_executor.execute_with_permission(
            "annotation_new",
            {"file_path": "notes.md", "file_content": "x", "annotation_content": "y"},
            tool_def, project_id="p",
        )
    assert result == "done"
    mock_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_library_tool_bypasses_write_approval():
    """library_new mutates the project DB, not the filesystem — exempt."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="created")):
        result = await permission_executor.execute_with_permission(
            "library_new", {"content_type": "md", "content": "x", "title": "t"},
            tool_def, project_id="p",
        )
    assert result == "created"


@pytest.mark.asyncio
async def test_draw_tool_bypasses_write_approval():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="drawn")):
        result = await permission_executor.execute_with_permission(
            "draw_image", {"prompt": "cat"},
            tool_def, project_id="p",
        )
    assert result == "drawn"


@pytest.mark.asyncio
async def test_task_tool_bypasses_write_approval():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="ok")):
        result = await permission_executor.execute_with_permission(
            "task_write", {"content": "x"},
            tool_def, project_id="p",
        )
    assert result == "ok"


# ── schema validation gate ──────────────────────────────────────────
#
# A structurally invalid call must be rejected before *any* permission dialog
# or tool execution. Otherwise the user approves and the tool errors anyway —
# the exact regression this layer prevents.

_EDIT_SCHEMA = {
    "type": "object",
    "required": ["file_path", "old_string", "new_string"],
    "properties": {
        "file_path": {"type": "string"},
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean"},
    },
}


@pytest.mark.asyncio
async def test_missing_required_arg_does_not_pause_or_execute():
    """edit without old_string/new_string -> error string, no pause, no call."""
    tool_def = _make_tool_def(is_read_only=False)
    tool_def.input_schema = _EDIT_SCHEMA
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock()) as mock_call, \
         patch.object(permission_executor.file_service, "check_write_allowed") \
         as mock_write:
        result = await permission_executor.execute_with_permission(
            "edit", {"file_path": "/p/f", "old_string": "a"},
            tool_def, project_id="p",
        )
    assert result.startswith("Error:")
    assert "new_string" in result
    mock_call.assert_not_called()
    mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_type_arg_does_not_pause():
    """replace_all must be boolean; a string is rejected pre-gate."""
    tool_def = _make_tool_def(is_read_only=False)
    tool_def.input_schema = _EDIT_SCHEMA
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock()) as mock_call:
        result = await permission_executor.execute_with_permission(
            "edit",
            {"file_path": "/p/f", "old_string": "a", "new_string": "b",
             "replace_all": "yes"},
            tool_def, project_id="p",
        )
    assert result.startswith("Error:")
    assert "replace_all" in result
    mock_call.assert_not_called()


# ── preflight gate (deterministic tool-specific checks) ─────────────

@pytest.mark.asyncio
async def test_preflight_error_short_circuits_before_dialog():
    """A tool whose preflight returns an error must not reach the gate or exec."""
    async def fail_preflight(**_kwargs):
        return "Error: preflight says no"
    tool_def = _make_tool_def(is_read_only=False, preflight=fail_preflight)
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock()) as mock_call, \
         patch.object(permission_executor.file_service, "check_write_allowed") \
         as mock_write:
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/p/f", "content": "x"},
            tool_def, project_id="p",
        )
    assert result == "Error: preflight says no"
    mock_call.assert_not_called()
    mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_preflight_none_proceeds_to_normal_gate():
    """preflight returning None must not change the existing gate behavior."""
    async def ok_preflight(**_kwargs):
        return None
    tool_def = _make_tool_def(is_read_only=False, preflight=ok_preflight)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.SANDBOX), \
         _auto_approve_patch("p", {"file_internal": False}):
        with pytest.raises(PermissionRequestPause):
            await permission_executor.execute_with_permission(
                "write", {"file_path": "/p/f", "content": "x"},
                tool_def, project_id="p",
            )


@pytest.mark.asyncio
async def test_schema_check_runs_before_preflight():
    """A missing arg is caught by the schema layer before preflight is invoked."""
    preflight_calls = []

    async def tracking_preflight(**kwargs):
        preflight_calls.append(kwargs)
        return None
    tool_def = _make_tool_def(is_read_only=False, preflight=tracking_preflight)
    tool_def.input_schema = _EDIT_SCHEMA
    result = await permission_executor.execute_with_permission(
        "edit", {"file_path": "/p/f"}, tool_def, project_id="p",
    )
    assert result.startswith("Error:")
    assert preflight_calls == []  # schema layer rejected before preflight ran


# ── real file-tools preflight integration ───────────────────────────

@pytest.mark.asyncio
async def test_edit_preflight_rejects_identical_old_new(tmp_path):
    """The edit tool's own preflight rejects old==new without pausing."""
    from app.agents.tools.registry import tool_registry
    edit_def = tool_registry.get("edit")
    assert edit_def is not None and edit_def.preflight is not None
    err = await edit_def.preflight(
        project_id="p", session_id="s",
        # A non-existent file is exempt from must-read-first, but the
        # identical old/new check must still reject it.
        file_path=str(tmp_path / "new.md"),
        old_string="x", new_string="x",
    )
    assert err is not None
    assert "identical" in err


@pytest.mark.asyncio
async def test_edit_preflight_passes_for_new_file(tmp_path):
    """A brand-new file (does not exist) is exempt from must-read-first."""
    from app.agents.tools.registry import tool_registry
    edit_def = tool_registry.get("edit")
    err = await edit_def.preflight(
        project_id="p", session_id="s",
        file_path=str(tmp_path / "new.md"),
        old_string="a", new_string="b",
    )
    assert err is None


@pytest.mark.asyncio
async def test_edit_preflight_rejects_unread_existing_file(tmp_path):
    """An existing file that was never read this session is rejected."""
    from app.agents.tools.registry import tool_registry
    target = tmp_path / "exists.md"
    target.write_text("hello")
    edit_def = tool_registry.get("edit")
    err = await edit_def.preflight(
        project_id="p", session_id="never-read-this-session",
        file_path=str(target), old_string="a", new_string="b",
    )
    assert err is not None
    assert "has not been read" in err


# ── notebook approval replay binding (sha256 of full cell source) ───

@pytest.mark.asyncio
async def test_notebook_pause_hashes_full_source_before_truncation():
    """The replay digest is taken over the FULL cell source: the dialog
    preview is truncated at CONTENT_SOFT_LIMIT, but the resume-side
    comparison must bind the content the user actually reviewed."""
    tool_def = _make_tool_def(is_read_only=False)
    source = "x = 1\n" + "y = 2\n" * (CONTENT_SOFT_LIMIT // 6)
    fake_notebook = {"cells": [{"id": "c1", "cell_type": "code", "source": [source]}]}
    fake_location = MagicMock()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(fake_notebook, fake_location))), \
         patch("app.agents.tools.notebook_utils.find_cell_index",
               return_value=0), \
         _auto_approve_patch("p", {"notebook": False}):
        with pytest.raises(PermissionRequestPause) as exc_info:
            await permission_executor.execute_with_permission(
                "notebook_run_cell",
                {"notebook_path": "nb.ipynb", "cell_id": "c1"},
                tool_def, project_id="p",
            )
    assert exc_info.value.content_truncated is True
    assert exc_info.value.content_sha256 == (
        hashlib.sha256(source.encode("utf-8")).hexdigest()
    )


@pytest.mark.asyncio
async def test_approved_notebook_drift_error_on_changed_cell():
    """A cell edited while the task was parked must not execute under the
    old approval — the user reviewed different code than what would run."""
    approved_source = "print('approved version')"
    changed = {"cells": [{"id": "c1", "cell_type": "code",
                          "source": "print('tampered')"}]}
    digest = hashlib.sha256(approved_source.encode("utf-8")).hexdigest()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(changed, MagicMock()))):
        err = await approved_notebook_drift_error(
            "p", {"notebook_path": "nb.ipynb", "cell_id": "c1"}, digest,
        )
    assert "changed since approval" in err
    assert "not executed" in err


@pytest.mark.asyncio
async def test_approved_notebook_drift_error_passes_when_cell_unchanged():
    source = "print('approved version')"
    notebook = {"cells": [{"id": "c1", "cell_type": "code", "source": source}]}
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(notebook, MagicMock()))):
        err = await approved_notebook_drift_error(
            "p", {"notebook_path": "nb.ipynb", "cell_id": "c1"}, digest,
        )
    assert err == ""


@pytest.mark.asyncio
async def test_approved_notebook_drift_error_on_removed_cell():
    """A cell deleted while parked cannot run either — the approved target
    is gone."""
    digest = hashlib.sha256(b"print('approved version')").hexdigest()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=({"cells": []}, MagicMock()))):
        err = await approved_notebook_drift_error(
            "p", {"notebook_path": "nb.ipynb", "cell_id": "c1"}, digest,
        )
    assert "removed since approval" in err
    assert "not executed" in err


@pytest.mark.asyncio
async def test_approved_notebook_drift_error_ignores_legacy_checkpoints():
    """Checkpoints persisted before the digest existed carry no
    content_sha256; the resume must proceed exactly as before instead of
    failing closed on an upgrade."""
    err = await approved_notebook_drift_error(
        "p", {"notebook_path": "nb.ipynb", "cell_id": "c1"}, "",
    )
    assert err == ""


# ── unexpected notebook read failure must not ask for blind approval ──

@pytest.mark.asyncio
async def test_notebook_unexpected_read_error_returns_error_not_pause():
    """A non-NotebookToolError read failure must surface as an error tool
    result — never as a pause whose dialog shows no code for the user to
    blindly approve."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(side_effect=OSError("disk error"))), \
         _auto_approve_patch("p", {"notebook": False}):
        result = await permission_executor.execute_with_permission(
            "notebook_run_cell",
            {"notebook_path": "nb.ipynb", "cell_id": "c1"},
            tool_def, project_id="p",
        )
    assert result.startswith("Error:")
    assert "c1" in result


# ── approved resume read-state restore (must-read after restart) ────

@pytest.mark.asyncio
async def test_write_approval_resume_after_restart_executes(project):
    """Restart simulation: the in-memory read-state cache is empty, yet an
    approved write to an existing file must execute — the pause implies the
    must-read preflight had already passed when the user approved. Without
    the restore, the tool's own gate consumes the approval with no effect."""
    session = "restart-write-sess"
    # Absolute paths keep the write tool fully functional in this harness
    # (write_file_absolute never consults the project registry).
    target = settings.get_project_path(project) / "report.md"
    target.write_text("# report\n")
    read_state_cache.clear(session)

    rejected = await _write_file(
        project_id=project, session_id=session,
        file_path=str(target), content="new body",
    )
    assert "has not been read" in rejected
    assert target.read_text() == "# report\n"

    restore_approved_read_state(
        "write", {"file_path": str(target)}, project, session,
    )
    result = await _write_file(
        project_id=project, session_id=session,
        file_path=str(target), content="new body",
    )
    assert result.startswith("File written")
    assert target.read_text() == "new body"


@pytest.mark.asyncio
async def test_edit_approval_resume_after_restart_executes(project):
    session = "restart-edit-sess"
    target = settings.get_project_path(project) / "code.py"
    target.write_text("value = 1\n")
    read_state_cache.clear(session)

    restore_approved_read_state(
        "edit", {"file_path": str(target)}, project, session,
    )
    result = await _edit_file(
        project_id=project, session_id=session, file_path=str(target),
        old_string="value = 1", new_string="value = 2",
    )
    assert result.startswith("File edited")
    assert target.read_text() == "value = 2\n"


@pytest.mark.asyncio
async def test_notebook_approval_resume_after_restart_passes_gate(project):
    """A notebook_run_cell approval resumed after a restart must get past the
    must-read gate; without a Jupyter server the call then stops at that next
    real dependency — anything else means the approval was consumed by the
    gate instead of the operation running."""
    session = "restart-nb-sess"
    notebook = {
        "cells": [{
            "cell_type": "code", "id": "c1", "metadata": {},
            "source": "1 + 1", "outputs": [], "execution_count": None,
        }],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }
    (settings.get_project_path(project) / "analysis.ipynb").write_text(
        json.dumps(notebook),
    )
    read_state_cache.clear(session)

    rejected = await _notebook_run_cell(
        notebook_path="analysis.ipynb", cell_id="c1",
        project_id=project, session_id=session,
    )
    assert "has not been read" in rejected

    restore_approved_read_state(
        "notebook_run_cell",
        {"notebook_path": "analysis.ipynb", "cell_id": "c1"},
        project, session,
    )
    result = await _notebook_run_cell(
        notebook_path="analysis.ipynb", cell_id="c1",
        project_id=project, session_id=session,
    )
    assert "Jupyter server is not running" in result


@pytest.mark.asyncio
async def test_approved_restore_scopes_to_approved_target(project):
    """Restoring one target must not un-gate the rest of the session: a new
    write to a file the model never read is still rejected after the resume."""
    session = "restore-scope-sess"
    base = settings.get_project_path(project)
    approved = base / "approved.md"
    approved.write_text("a")
    other = base / "other.md"
    other.write_text("untouched\n")
    read_state_cache.clear(session)

    restore_approved_read_state(
        "write", {"file_path": str(approved)}, project, session,
    )
    rejected = await _write_file(
        project_id=project, session_id=session, file_path=str(other),
        content="x",
    )
    assert "has not been read" in rejected
    assert other.read_text() == "untouched\n"

