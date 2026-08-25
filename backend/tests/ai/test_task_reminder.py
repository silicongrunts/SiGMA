"""Task reminders snapshot onto a user turn at submit time; message rows stay final."""

from types import SimpleNamespace

import pytest

import app.services.ai_service as ai_service_module
import app.services.query_loop as query_loop_module
import app.services.task_service as task_service_module
from app.core.message_format import STATUS_TAG_RE
from app.services.ai_service import ai_service
from app.services.query_loop import QueryLoop
from app.services.task_service import (
    TASK_REMINDER_HEADER,
    render_task_reminder_tag,
    strip_task_reminder,
    unfinished_tasks,
)


def _task_row(task_id, subject, status):
    return SimpleNamespace(
        id=task_id, subject=subject, description="",
        status=status, metadata_json=None,
    )


class _TasksRepo:
    def __init__(self, rows):
        self.rows = rows

    async def list_active(self, session_id):
        return self.rows


class _TaskUow:
    def __init__(self, tasks):
        self.tasks = tasks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unfinished_tasks_excludes_completed(monkeypatch):
    monkeypatch.setattr(
        task_service_module, "UnitOfWork",
        lambda pid: _TaskUow(_TasksRepo([
            _task_row("1", "Write outline", "completed"),
            _task_row("2", "Translate chapter", "pending"),
            _task_row("3", "Add references", "in_progress"),
        ])),
    )

    tasks = await unfinished_tasks("p1", "s1")

    assert [t["id"] for t in tasks] == ["2", "3"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_content_snapshots_reminder_into_persisted_message(monkeypatch):
    async def _unfinished(project_id, session_id):
        return [{"id": "7", "subject": "Caption figures", "status": "pending"}]
    monkeypatch.setattr(ai_service_module, "unfinished_tasks", _unfinished)

    content = await ai_service._build_user_message_content(
        "please continue", {}, "p1", "s1",
    )

    assert "[7] Caption figures (pending)" in content
    # The reminder rides the hidden <status> blocks — the chat UI never shows it.
    assert STATUS_TAG_RE.sub("", content).strip() == "please continue"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_content_omits_reminder_without_unfinished_tasks(monkeypatch):
    async def _unfinished(project_id, session_id):
        return []
    monkeypatch.setattr(ai_service_module, "unfinished_tasks", _unfinished)

    content = await ai_service._build_user_message_content(
        "please continue", {}, "p1", "s1",
    )

    assert TASK_REMINDER_HEADER not in content


@pytest.mark.unit
def test_strip_restores_pre_reminder_content():
    original = "<status>\ncurrent_time: x\n</status>\nwhat is this"
    persisted = (
        "<status>\ncurrent_time: x\n</status>"
        + render_task_reminder_tag([
            {"id": "1", "subject": "Caption figures", "status": "pending"},
        ])
        + "\nwhat is this"
    )

    assert strip_task_reminder(persisted) == original


class _MsgRow:
    def __init__(self, role, content):
        self.role = role
        self.content = content
        self.input_tokens = 0
        self.tool_calls = None
        self.tool_call_id = ""
        self.reasoning_content = ""


class _HistoryRepo:
    def __init__(self, rows):
        self.rows = rows

    async def get_messages_for_llm(self, session_id):
        return list(self.rows)


class _ConfigRepo:
    async def get(self, key, default=""):
        return default


class _PoisonTasksRepo:
    """Fails the test if message building ever queries task state."""

    def list_active(self, session_id):
        raise AssertionError("message building must not read tasks")


class _BuildUow:
    config_repo = _ConfigRepo()
    messages_repo = None
    tasks_repo = _PoisonTasksRepo()

    def __init__(self, project_id):
        self.config = self.config_repo
        self.messages = self.messages_repo
        self.tasks = self.tasks_repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_messages_is_pure_over_persisted_history(monkeypatch):
    # The reminder was snapshotted at submit time, so request building must
    # be a pure read of persisted rows: rebuilds stay byte-identical and
    # prompt-cache hits survive no matter how task state changes mid-turn.
    persisted = (
        "<status>\ncurrent_time: x\n</status>"
        + render_task_reminder_tag([
            {"id": "9", "subject": "Draft conclusion", "status": "pending"},
        ])
        + "\nnew request"
    )
    _BuildUow.messages_repo = _HistoryRepo([
        _MsgRow("user", "keep going"),
        _MsgRow("assistant", "ok"),
        _MsgRow("user", persisted),
    ])
    monkeypatch.setattr(query_loop_module, "UnitOfWork", _BuildUow)
    monkeypatch.setattr(query_loop_module, "model_role_accepts_images", lambda role: False)
    monkeypatch.setattr(query_loop_module, "settings", SimpleNamespace(
        get_project_path=lambda project_id: "/tmp",
    ))
    monkeypatch.setattr(query_loop_module, "prompt_service", SimpleNamespace(
        build_system_prompt=lambda **kwargs: "SYS",
    ))
    monkeypatch.setattr(query_loop_module, "session_temp_service", SimpleNamespace(
        session_dir_for_prompt=lambda project_id, session_id: "/tmp/s1",
    ))

    from app.services.skill_service import skill_service
    monkeypatch.setattr(skill_service, "build_skills_prompt", lambda: "")
    from app.services.project_service import project_service
    monkeypatch.setattr(project_service, "get_project_meta", lambda project_id: {
        "name": "n", "description": "d",
    })

    loop = QueryLoop(project_id="p1", session_id="s1")

    first = await loop._build_messages()
    second = await loop._build_messages()

    assert first == second
    # system + 3 history rows — the reminder adds no extra message.
    assert len(first) == 4
    assert "[9] Draft conclusion (pending)" in first[-1]["content"]
