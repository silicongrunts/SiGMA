from types import SimpleNamespace

import pydantic
import pytest

from app.models.requests import EditChatMessageRequest, ForkSessionRequest, StreamChatRequest, UpdateSessionRequest
from app.routes import chat
from tests.factories.uploads import ChunkedUpload


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
            "user_state": {"tab": "synthesis"},
            "attachments": [{"path": "image.png"}],
            "token_budget": 100,
        },
        {"session_id": "session-1", "resume": False, "interaction_response": None},
    )


@pytest.mark.route
def test_interaction_response_requires_interaction_id():
    """A permission response without interaction_id fails schema validation
    with a missing-field error. (task_id/interaction_id are plain strings in
    the schema — there is no ownership cross-check at the request layer.)"""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        StreamChatRequest(
            resume=True,
            session_id="session-1",
            interaction_response={
                "task_id": "task-1",
                "interaction_type": "permission",
                "approved": True,
            },
        )

    assert any(
        error["type"] == "missing" and error["loc"][-1] == "interaction_id"
        for error in exc_info.value.errors()
    )


@pytest.mark.route
def test_interaction_response_approved_must_be_strict_boolean():
    """`approved` is a StrictBool: a truthy string like "yes" is rejected
    instead of being coerced to True."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        StreamChatRequest(
            resume=True,
            session_id="session-1",
            interaction_response={
                "task_id": "task-1",
                "interaction_id": "interaction-1",
                "interaction_type": "permission",
                "approved": "yes",
            },
        )

    assert any(
        error["type"] == "bool_type" and error["loc"][-1] == "approved"
        for error in exc_info.value.errors()
    )


@pytest.mark.route
@pytest.mark.parametrize("interaction_response", [
    {
        "task_id": "task-1", "interaction_id": "interaction-1",
        "interaction_type": "permission", "approved": True, "legacy": 1,
    },
    {
        "task_id": "task-1", "interaction_id": "interaction-1",
        "interaction_type": "ask_user_question",
        "answers": [{"question": "q", "answer": "a"}], "legacy": 1,
    },
    {
        "task_id": "task-1", "interaction_id": "interaction-1",
        "interaction_type": "submit_plan_for_approval", "approved": True,
        "legacy": 1,
    },
])
def test_every_interaction_response_rejects_unknown_fields(interaction_response):
    with pytest.raises(pydantic.ValidationError):
        StreamChatRequest(
            resume=True,
            session_id="session-1",
            interaction_response=interaction_response,
        )


@pytest.mark.route
def test_question_answer_rejects_unknown_fields():
    with pytest.raises(pydantic.ValidationError):
        StreamChatRequest(
            resume=True,
            session_id="session-1",
            interaction_response={
                "task_id": "task-1",
                "interaction_id": "interaction-1",
                "interaction_type": "ask_user_question",
                "answers": [{"question": "q", "answer": "a", "legacy": 1}],
            },
        )

    with pytest.raises(pydantic.ValidationError):
        StreamChatRequest(
            resume=True,
            session_id="session-1",
            interaction_response={
                "task_id": "task-1",
                "interaction_id": "interaction-1",
                "interaction_type": "permission",
                "approved": 1,
            },
        )


@pytest.mark.route
@pytest.mark.asyncio
async def test_resume_stream_replays_only_events_after_cursor():
    """Reconnect semantics for GET /chat/stream/{task_id}.

    A fresh subscriber (cursor omitted) is replayed the whole buffer. A
    reconnecting client reporting the highest event id it already received
    gets the chunks pushed since then — and none of the already-delivered
    ones. The terminal ``done`` event closes both streams, so no artificial
    teardown is needed.
    """
    from app.services import stream_hub as stream_hub_module

    session = stream_hub_module.stream_hub.create("task-resume-1", project_id="project-1")
    try:
        session.push({"type": "message", "data": {"text": "one"}})    # id 1
        session.push({"type": "message", "data": {"text": "two"}})    # id 2
        session.push({"type": "done", "data": {"ok": True}})          # id 3, terminal

        fresh = await chat.resume_stream("task-resume-1", cursor=None, project_id="project-1")
        fresh_frames = [frame async for frame in fresh.body_iterator]

        # The client reconnects claiming it has everything through id 2.
        resumed = await chat.resume_stream("task-resume-1", cursor=2, project_id="project-1")
        assert resumed.media_type == "text/event-stream"
        resumed_frames = [frame async for frame in resumed.body_iterator]
    finally:
        stream_hub_module.stream_hub.remove("task-resume-1", session)

    # Every stream opens by naming the task.
    task_id_frame = 'event: task_id\ndata: {"task_id": "task-resume-1"}\n\n'
    assert fresh_frames[0] == task_id_frame
    assert resumed_frames[0] == task_id_frame

    def _ids(frames):
        return [line for frame in frames for line in frame.splitlines() if line.startswith("id:")]

    # Fresh subscriber: the full buffer, ids 1..3 in order.
    assert _ids(fresh_frames) == ["id: 1", "id: 2", "id: 3"]

    # Reconnecting client: only the offline chunk (id 3, the terminal done),
    # never a replay of the already-delivered ids 1 and 2.
    assert _ids(resumed_frames) == ["id: 3"]
    assert resumed_frames[1] == 'id: 3\nevent: done\ndata: {"ok": true}\n\n'
    assert len(resumed_frames) == 2  # task_id + the single buffered chunk


@pytest.mark.route
@pytest.mark.asyncio
async def test_cancel_task_validates_project_before_cancel(monkeypatch, project_gate):
    calls = []
    project_gate(monkeypatch, chat, calls)

    async def cancel_task(project_id, task_id):
        calls.append(("cancel", project_id, task_id))
        return {"cancelled": True, "status": "cancelling", "task_id": task_id}

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


@pytest.mark.route
@pytest.mark.asyncio
async def test_search_chat_passes_query_and_wraps_result(monkeypatch):
    calls = {}

    async def search_chat(project_id, q):
        calls["search"] = (project_id, q)
        return {"query": q, "groups": [], "total_matches": 0, "total_sessions": 0}

    monkeypatch.setattr(chat, "ai_service", SimpleNamespace(search_chat=search_chat))

    result = await chat.search_chat("project-1", "needle")

    assert result["success"] is True
    assert result["data"]["query"] == "needle"
    assert calls["search"] == ("project-1", "needle")


@pytest.mark.route
@pytest.mark.asyncio
async def test_fork_session_passes_message_and_returns_session(monkeypatch):
    calls = {}

    async def fork_session(project_id, session_id, message_id, title=""):
        calls["fork"] = (project_id, session_id, message_id, title)
        return {"id": "fork-1", "title": "Forked"}

    monkeypatch.setattr(
        chat,
        "ai_service",
        SimpleNamespace(fork_session=fork_session),
    )

    result = await chat.fork_session(
        "project-1",
        "session-1",
        ForkSessionRequest(message_id="msg-1", title="Forked"),
    )

    assert result["success"] is True
    assert result["data"] == {"id": "fork-1", "title": "Forked"}
    assert calls["fork"] == ("project-1", "session-1", "msg-1", "Forked")


@pytest.mark.route
@pytest.mark.asyncio
async def test_upload_chat_attachment_rejects_oversize_during_read(monkeypatch):
    """An upload over the image cap must fail fast with the same 413 error
    save_chat_image raises, without saving any attachment file."""
    from app.core import uploads
    from app.core.exceptions import FileSystemError

    saved = []

    async def save_chat_image(**kwargs):
        saved.append(kwargs)
        return {"path": "x.png"}

    monkeypatch.setattr(chat, "save_chat_image", save_chat_image)
    monkeypatch.setattr(chat, "MAX_CHAT_IMAGE_BYTES", 8)
    monkeypatch.setattr(uploads, "UPLOAD_READ_CHUNK_BYTES", 4)

    upload = ChunkedUpload(
        b"123456789", filename="image.png", content_type="image/png",
    )

    with pytest.raises(FileSystemError) as exc_info:
        await chat.upload_chat_attachment("project-1", "session-1", upload)

    assert exc_info.value.status_code == 413
    assert exc_info.value.code == "INVALID_REQUEST"
    assert saved == []
    # 8 bytes (two chunks) hit the cap; the raise happens while reading the
    # third chunk, so the loop stops there instead of draining the body.
    assert upload.read_calls == 3


@pytest.mark.route
@pytest.mark.asyncio
async def test_upload_chat_attachment_under_cap_saves_image(monkeypatch):
    from app.core import uploads

    calls = {}

    async def save_chat_image(**kwargs):
        calls.update(kwargs)
        return {"path": ".SiGMA/chat_attachments/a.png", "size": 4}

    monkeypatch.setattr(chat, "save_chat_image", save_chat_image)
    monkeypatch.setattr(uploads, "UPLOAD_READ_CHUNK_BYTES", 2)

    upload = ChunkedUpload(
        b"1234", filename="image.png", content_type="image/png",
    )

    result = await chat.upload_chat_attachment("project-1", "session-1", upload)

    assert result["success"] is True
    assert result["data"]["path"] == ".SiGMA/chat_attachments/a.png"
    assert calls["project_id"] == "project-1"
    assert calls["session_id"] == "session-1"
    assert calls["filename"] == "image.png"
    assert calls["content"] == b"1234"
    assert calls["mime_type"] == "image/png"
