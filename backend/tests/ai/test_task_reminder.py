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
from tests.ai.conftest import (
    FakeConfigRepo,
    RecordingMessagesRepo,
    make_fake_uow,
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unfinished_tasks_excludes_completed(monkeypatch):
    monkeypatch.setattr(
        task_service_module, "UnitOfWork",
        make_fake_uow(tasks=_TasksRepo([
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


class _PoisonTasksRepo:
    """Stays local: a poison stub that fails the test if message building
    ever queries task state."""

    def list_active(self, session_id):
        raise AssertionError("message building must not read tasks")


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
    uow_cls = make_fake_uow(
        config=FakeConfigRepo(),
        messages=RecordingMessagesRepo(history=[
            _MsgRow("user", "keep going"),
            _MsgRow("assistant", "ok"),
            _MsgRow("user", persisted),
        ]),
        tasks=_PoisonTasksRepo(),
    )
    monkeypatch.setattr(query_loop_module, "UnitOfWork", uow_cls)
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
