"""
Unit tests for the shared permission executor.

Covers the single source of truth that both QueryLoop (main loop) and
agent_service (subagents) delegate to. Tests assert that:
- read-only tools bypass approval entirely
- bash routes through check_bash_permission_async (read-only auto-approve,
  non-read-only asks the user)
- notebook_run_cell reads cell source code as preview content
- write inside sandbox is allowed, outside triggers approval
- ``edit`` composes a before/after diff as preview content
- ``permission_requester is None`` uniformly denies (no sandbox fallback)
- user denial surfaces the optional reason back to the LLM
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
    def __init__(self, approved: bool, reason: str = ""):
        self.approved = approved
        self.reason = reason


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
    with patch.object(permission_executor, "check_bash_permission_async",
                      new=AsyncMock(return_value=_BashResult(True))) as mock_check, \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="file.txt")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "ls", "description": "list files"},
            tool_def, project_id="p", permission_requester=AsyncMock(),
        )
    assert result == "file.txt"
    mock_check.assert_awaited_once()
    # description must be forwarded so the dialog can display it
    _, kwargs = mock_check.call_args
    assert kwargs["description"] == "list files"


@pytest.mark.asyncio
async def test_bash_non_readonly_asks_user_and_approved():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor, "check_bash_permission_async",
                      new=AsyncMock(return_value=_BashResult(False, "needs approval"))):
        # When check_bash_permission_async returns not-approved, the async
        # wrapper itself drives the requester — so we let the real function
        # run. Re-patch to the real one with requester flowing through.
        pass
    # The above is too indirect; use the real async wrapper end-to-end:
    from app.agents.tools.bash_permissions import check_bash_permission_async
    with patch("app.services.permission_executor.check_bash_permission_async",
               new=check_bash_permission_async), \
         patch("app.agents.tools.bash_permissions.check_bash_permission",
               return_value=_BashResult(False, "needs approval")), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="done")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "rm /tmp/x", "description": "cleanup"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "done"
    requester.assert_awaited_once()
    _, kwargs = requester.call_args
    assert kwargs["description"] == "cleanup"


@pytest.mark.asyncio
async def test_bash_user_denied_returns_reason():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_denied_resp("dangerous"))
    from app.agents.tools.bash_permissions import check_bash_permission_async
    with patch("app.services.permission_executor.check_bash_permission_async",
               new=check_bash_permission_async), \
         patch("app.agents.tools.bash_permissions.check_bash_permission",
               return_value=_BashResult(False, "needs approval")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "rm /"}, tool_def,
            project_id="p", permission_requester=requester,
        )
    assert "User rejected" in result
    assert "dangerous" in result


@pytest.mark.asyncio
async def test_bash_no_requester_denies():
    tool_def = _make_tool_def(is_read_only=False)
    from app.agents.tools.bash_permissions import check_bash_permission_async
    with patch("app.services.permission_executor.check_bash_permission_async",
               new=check_bash_permission_async), \
         patch("app.agents.tools.bash_permissions.check_bash_permission",
               return_value=_BashResult(False, "needs approval")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": "rm /"}, tool_def,
            project_id="p", permission_requester=None,
        )
    assert "no permission channel" in result


@pytest.mark.asyncio
async def test_bash_empty_command_skips_check():
    """Empty command path is a degenerate case — no check, proceed to call."""
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor, "check_bash_permission_async",
                      new=AsyncMock()) as mock_check, \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="noop")):
        result = await permission_executor.execute_with_permission(
            "bash", {"command": ""}, tool_def,
            project_id="p", permission_requester=None,
        )
    assert result == "noop"
    mock_check.assert_not_awaited()


# ── notebook_run_cell ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notebook_run_reads_cell_source_as_content():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    fake_notebook = {"cells": [{"id": "c1", "cell_type": "code", "source": ["print('hi')\n"]}]}
    fake_location = MagicMock()  # read_notebook_json now returns (notebook, location)
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(fake_notebook, fake_location))), \
         patch("app.agents.tools.notebook_utils.find_cell_index",
               return_value=0), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="executed")):
        result = await permission_executor.execute_with_permission(
            "notebook_run_cell",
            {"notebook_path": "nb.ipynb", "cell_id": "c1"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "executed"
    _, kwargs = requester.call_args
    assert "print('hi')" in kwargs["content"]


@pytest.mark.asyncio
async def test_notebook_run_no_requester_denies():
    tool_def = _make_tool_def(is_read_only=False)
    # Mock the notebook read so we exercise the requester-None branch directly,
    # rather than the NotebookToolError early-return path.
    fake_notebook = {"cells": [{"id": "c1", "cell_type": "code", "source": "print('hi')\n"}]}
    fake_location = MagicMock()
    with patch("app.agents.tools.notebook_utils.read_notebook_json",
               new=AsyncMock(return_value=(fake_notebook, fake_location))), \
         patch("app.agents.tools.notebook_utils.find_cell_index",
               return_value=0):
        result = await permission_executor.execute_with_permission(
            "notebook_run_cell",
            {"notebook_path": "nb.ipynb", "cell_id": "c1"},
            tool_def, project_id="p", permission_requester=None,
        )
    assert "Cannot execute notebook code" in result
    assert "unavailable" in result


# ── write permissions ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_inside_sandbox_auto_approved():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.SANDBOX), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="written")):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/p/file.txt", "content": "x"},
            tool_def, project_id="p",
            permission_requester=AsyncMock(),
        )
    assert result == "written"


@pytest.mark.asyncio
async def test_write_outside_sandbox_asks_user():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
         patch.object(permission_executor.LLMLoopRunner, "call_tool",
                      new=AsyncMock(return_value="written")):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/etc/cron.d/x", "content": "x"},
            tool_def, project_id="p", permission_requester=requester,
        )
    assert result == "written"
    requester.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_outside_sandbox_no_requester_denies():
    tool_def = _make_tool_def(is_read_only=False)
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/etc/cron.d/x"}, tool_def,
            project_id="p", permission_requester=None,
        )
    assert "Cannot write to '/etc/cron.d/x'" in result
    assert "unavailable" in result


@pytest.mark.asyncio
async def test_edit_composes_old_new_diff_as_content():
    tool_def = _make_tool_def(is_read_only=False)
    requester = AsyncMock(return_value=_approved_resp())
    with patch.object(permission_executor.file_service, "check_write_allowed",
                      return_value=PathAccessLevel.EXTERNAL), \
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
                      return_value=PathAccessLevel.EXTERNAL):
        result = await permission_executor.execute_with_permission(
            "write", {"file_path": "/etc/x"}, tool_def,
            project_id="p", permission_requester=requester,
        )
    assert "User denied permission to write to file: /etc/x" in result
    assert "protected path" in result


# ── annotation tools never trigger the write-approval dialog ────────

@pytest.mark.asyncio
async def test_annotation_tool_bypasses_write_approval():
    """Annotation tools never trigger the write-approval dialog, even when
    marked ``is_read_only=False``. Path containment is the tool layer's
    responsibility, so the executor unconditionally lets them through."""
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
