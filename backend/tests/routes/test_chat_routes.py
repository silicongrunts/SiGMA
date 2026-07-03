from types import SimpleNamespace

import pytest

from app.models.requests import EditChatMessageRequest, StreamChatRequest, UpdateSessionRequest
from app.routes import chat


async def _empty_stream():
    if False:
        yield b""


@pytest.mark.route
@pytest.mark.asyncio
async def test_stream_chat_passes_context_and_session(monkeypatch):
    calls = {}

    async def submit_chat(project_id, message, context, **kwargs):
        calls["submit"] = (project_id, message, context, kwargs)
        return {"task_id": "task-1"}

    monkeypatch.setattr(
        chat,
        "ai_service",
        SimpleNamespace(submit_chat=submit_chat, sse_listen=lambda task_id, project_id=None: _empty_stream()),
    )

    response = await chat.stream_chat(
        "project-1",
        StreamChatRequest(
            message="hello",
            session_id="session-1",
            file="paper.tex",
            token_budget=100,
            attachments=[{"path": "image.png"}],
            user_state={"tab": "synthesis"},
        ),
    )

    assert response.media_type == "text/event-stream"
    assert calls["submit"] == (
        "project-1",
        "hello",
        {
            "file": "paper.tex",
            "user_state": {"tab": "synthesis"},
            "attachments": [{"path": "image.png"}],
            "token_budget": 100,
        },
        {"session_id": "session-1", "resume": False, "interaction_response": None},
    )


@pytest.mark.route
@pytest.mark.asyncio
async def test_cancel_task_validates_project_before_cancel(monkeypatch):
    calls = []

    async def cancel_task(project_id, task_id):
        calls.append(("cancel", project_id, task_id))
        return {"cancelled": True, "status": "cancelling", "task_id": task_id}

    monkeypatch.setattr(
        chat,
        "project_service",
        SimpleNamespace(get_project_path=lambda project_id: calls.append(("project", project_id))),
    )
    monkeypatch.setattr(chat, "ai_service", SimpleNamespace(cancel_task=cancel_task))

    result = await chat.cancel_task("project-1", "task-1")

    assert result["data"] == {
        "cancelled": True,
        "status": "cancelling",
        "task_id": "task-1",
    }
    # Project validation precedes the cancel call.
    assert calls == [("project", "project-1"), ("cancel", "project-1", "task-1")]


@pytest.mark.route
@pytest.mark.asyncio
async def test_update_session_passes_optional_fields(monkeypatch):
    calls = {}

    async def update_session(project_id, session_id, **kwargs):
        calls["update"] = (project_id, session_id, kwargs)

    monkeypatch.setattr(chat, "ai_service", SimpleNamespace(update_session=update_session))

    result = await chat.update_session(
        "project-1",
        "session-1",
        UpdateSessionRequest(title="New", is_archived=True),
    )

    assert result["success"] is True
    assert calls["update"] == (
        "project-1",
        "session-1",
        {"title": "New", "is_archived": True},
    )


@pytest.mark.route
@pytest.mark.asyncio
async def test_edit_chat_message_passes_replace_request(monkeypatch):
    calls = {}

    async def edit_and_submit_chat(**kwargs):
        calls.update(kwargs)
        return {"task_id": "task-2"}

    monkeypatch.setattr(
        chat,
        "ai_service",
        SimpleNamespace(edit_and_submit_chat=edit_and_submit_chat, sse_listen=lambda task_id, project_id=None: _empty_stream()),
    )

    response = await chat.edit_chat_message(
        "project-1",
        "session-1",
        EditChatMessageRequest(
            message_id="msg-1",
            message="replacement",
            token_budget=50,
        ),
    )

    assert response.media_type == "text/event-stream"
    assert calls == {
        "project_id": "project-1",
        "session_id": "session-1",
        "message_id": "msg-1",
        "message": "replacement",
        "context": {"user_state": None, "attachments": [], "token_budget": 50},
    }
