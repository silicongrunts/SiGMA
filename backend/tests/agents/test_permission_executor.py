"""
Unit tests for the shared permission executor.

Covers the four-category permission model:
- ``file_external`` / ``file_internal``: filesystem writes classified by path
- ``bash``: non-read-only shell commands
- ``notebook``: executing a notebook cell

Auto-approve is read live from the project DB. When a category's flag is on the
executor silently approves (no requester call); otherwise the frontend dialog
is invoked via the requester. Read-only tools and the exempt tool families
(library/draw/task/annotation) bypass approval entirely.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import permission_executor
from app.services.file_service import PathAccessLevel


# ── helpers ─────────────────────────────────────────────────────────

def _make_tool_def(*, is_read_only=True, has_call=True):
    tool_def = MagicMock()
    tool_def.is_read_only = is_read_only
    tool_def.call = MagicMock() if has_call else None
    return tool_def


def _approved_resp():
    return {"approved": True, "reason": ""}


def _denied_resp(reason=""):
    return {"approved": False, "reason": reason}


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
            "nope", {}, None, project_id="p", permission_requester=None,
        )
    assert "Unknown tool" in result


@pytest.mark.asyncio
async def test_tool_without_call_returns_error_string():
    tool_def = _make_tool_def(has_call=False)
    result = await permission_executor.execute_with_permission(
        "ghost", {}, tool_def, project_id="p", permission_requester=None,
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
            project_id="p", permission_requester=None,
        )
    assert result == "data"
    mock_call.assert_awaited_once()


# ── bash ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_readonly_auto_approved():
    """ls is read-only per the allowlist; no requester call."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(True)) as mock_check, \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="file.txt")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "ls", "description": "list files"},
            tool_def, project_id="p", permission_requester=AsyncMock(),
        )
    assert result == "file.txt"
    mock_check.assert_called_once_with("ls")


@pytest.mark.asyncio
async def test_bash_non_readonly_auto_approve_silently_passes():
    """When bash auto-approve is on, no requester call is made."""
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(False, "needs approval")), \
         _auto_approve_patch("p", {"bash": True}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="done")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "rm /tmp/x", "description": "cleanup"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "done"
    requester.assert_not_awaited()


@pytest.mark.asyncio
async def test_bash_non_readonly_asks_user_and_approved():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(False, "needs approval")), \
         _auto_approve_patch("p", {"bash": False}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="done")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "rm /tmp/x", "description": "cleanup"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "done"
    requester.assert_awaited_once()
    _, kwargs = requester.call_args
    assert kwargs["tool"] == "bash"
    assert kwargs["description"] == "cleanup"


@pytest.mark.asyncio
async def test_bash_user_denied_returns_reason():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_denied_resp("dangerous"))
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(False, "needs approval")), \
         _auto_approve_patch("p", {"bash": False}):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "rm /"}, tool_def,
            project_id="p", permission_requester=requester,
        )
    assert "User rejected" in result
    assert "dangerous" in result


@pytest.mark.asyncio
async def test_bash_no_requester_denies():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor, "check_bash_permission",
                      return_value=_BashResult(False, "needs approval")), \
         _auto_approve_patch("p", {"bash": False}):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "rm /"}, tool_def,
            project_id="p", permission_requester=None,
        )
    assert "unavailable" in result


@pytest.mark.asyncio
async def test_bash_empty_command_skips_check():
    """Empty command path is a degenerate case — no check, proceed to call."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor, "check_bash_permission") as mock_check, \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="noop")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": ""}, tool_def,
            project_id="p", permission_requester=None,
        )
    assert result == "noop"
    mock_check.assert_not_called()


# ── notebook_run_cell ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notebook_run_reads_cell_source_as_content():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    fake_notebook = {"cells": [{"id": "c1", "cell_type": "code", "source": ["print('hi')\n"]}]}
    fake_location = MagicMock()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(fake_notebook, fake_location))), \
         patch("app.agents.tools.notebook_utils.find_cell_index",
               return_value=0), \
         _auto_approve_patch("p", {"notebook": False}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="executed")):
        result = await permission_executor.execute_with_permission(
            "notebook_run_cell",
            {"notebook_path": "nb.ipynb", "cell_id": "c1"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "executed"
    _, kwargs = requester.call_args
    assert kwargs["tool"] == "notebook"
    assert "print('hi')" in kwargs["content"]


@pytest.mark.asyncio
async def test_notebook_run_auto_approve_skips_requester():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
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
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "executed"
    requester.assert_not_awaited()


@pytest.mark.asyncio
async def test_notebook_run_no_requester_denies():
    tool_def = _make_tool_def(is_read_only=False)
    fake_notebook = {"cells": [{"id": "c1", "cell_type": "code", "source": "print('hi')\n"}]}
    fake_location = MagicMock()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(fake_notebook, fake_location))), \
         patch("app.agents.tools.notebook_utils.find_cell_index",
               return_value=0), \
         _auto_approve_patch("p", {"notebook": False}):
        result = await permission_executor.execute_with_permission(
            "notebook_run_cell",
            {"notebook_path": "nb.ipynb", "cell_id": "c1"},
            tool_def, project_id="p", permission_requester=None,
        )
    assert "Cannot execute notebook code" in result
    assert "unavailable" in result


# ── file_external / file_internal writes ────────────────────────────

@pytest.mark.asyncio
async def test_write_inside_sandbox_auto_approve_off_asks_user():
    """B model: internal writes need approval unless auto-approve is on."""
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.SANDBOX), \
         _auto_approve_patch("p", {"file_internal": False}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="written")):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/p/file.txt", "content": "x"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "written"
    requester.assert_awaited_once()
    _, kwargs = requester.call_args
    assert kwargs["tool"] == "file_internal"


@pytest.mark.asyncio
async def test_write_inside_sandbox_auto_approve_on_silent():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.SANDBOX), \
         _auto_approve_patch("p", {"file_internal": True}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="written")):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/p/file.txt", "content": "x"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "written"
    requester.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_outside_sandbox_asks_user_with_external_category():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         _auto_approve_patch("p", {"file_external": False}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="written")):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/home/x.txt", "content": "x"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "written"
    requester.assert_awaited_once()
    _, kwargs = requester.call_args
    assert kwargs["tool"] == "file_external"


@pytest.mark.asyncio
async def test_write_outside_sandbox_no_requester_denies():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         _auto_approve_patch("p", {"file_external": False}):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/home/x.txt"}, tool_def,
            project_id="p", permission_requester=None,
        )
    assert "Cannot write to '/home/x.txt'" in result
    assert "unavailable" in result


@pytest.mark.asyncio
async def test_edit_composes_old_new_diff_as_content():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         _auto_approve_patch("p", {"file_external": False}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="edited")):
        await permission_executor.execute_with_permission(
            "edit",
            {"file_path": "/p/f", "old_string": "a", "new_string": "b"},
            tool_def, project_id="p", permission_requester=requester,
        )
    _, kwargs = requester.call_args
    assert "--- before ---" in kwargs["content"]
    assert "--- after ---" in kwargs["content"]


@pytest.mark.asyncio
async def test_write_user_denied_returns_reason():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_denied_resp("protected path"))
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         _auto_approve_patch("p", {"file_external": False}):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/etc/x"}, tool_def,
            project_id="p", permission_requester=requester,
        )
    assert "User denied permission to write to file: /etc/x" in result
    assert "protected path" in result


@pytest.mark.asyncio
async def test_tmp_treated_as_internal_category():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.TMP), \
         _auto_approve_patch("p", {"file_internal": False}), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="written")):
        await permission_executor.execute_with_permission(
            "write", {"file_path": "/tmp/x"}, tool_def,
            project_id="p", permission_requester=requester,
        )
    _, kwargs = requester.call_args
    assert kwargs["tool"] == "file_internal"


# ── annotation / library / draw / task tools are exempt ─────────────

@pytest.mark.asyncio
async def test_annotation_tool_bypasses_write_approval():
    """Exempt tools never trigger approval even when ``is_read_only=False``."""
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="done")) as mock_call:
        result = await permission_executor.execute_with_permission(
            "annotation_new",
            {"file_path": "notes.md", "file_content": "x", "annotation_content": "y"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "done"
    mock_call.assert_awaited_once()
    requester.assert_not_awaited()


@pytest.mark.asyncio
async def test_library_tool_bypasses_write_approval():
    """library_new mutates the project DB, not the filesystem — exempt."""
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="created")):
        result = await permission_executor.execute_with_permission(
            "library_new", {"content_type": "md", "content": "x", "title": "t"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "created"
    requester.assert_not_awaited()


@pytest.mark.asyncio
async def test_draw_tool_bypasses_write_approval():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="drawn")):
        result = await permission_executor.execute_with_permission(
            "draw_image", {"prompt": "cat"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "drawn"
    requester.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_tool_bypasses_write_approval():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="ok")):
        result = await permission_executor.execute_with_permission(
            "task_write", {"content": "x"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "ok"
    requester.assert_not_awaited()
