from fastapi import APIRouter, Query, UploadFile, File
from fastapi.responses import StreamingResponse

from app.services.ai_service import ai_service
from app.services.chat_attachments import save_chat_image
from app.services.project_service import project_service
from app.models.requests import StreamChatRequest, UpdateSessionRequest, EditChatMessageRequest, SkillLoadRequest
from app.core.response import ok

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/stream/{project_id}")
async def stream_chat(project_id: str, data: StreamChatRequest):
    """Send a message and receive SSE streamed response."""
    context = {
        "file": data.file,
        "user_state": data.user_state,
        "attachments": data.attachments,
        "token_budget": data.token_budget,
    }
    result = await ai_service.submit_chat(
        project_id, data.message, context,
        session_id=data.session_id, resume=data.resume,
        interaction_response=data.interaction_response,
    )
    return StreamingResponse(
        ai_service.sse_listen(result["task_id"], project_id=project_id),
        media_type="text/event-stream",
    )


@router.get("/stream/{task_id}")
async def resume_stream(task_id: str):
    """Reconnect to the SSE stream of an existing background task."""
    return StreamingResponse(
        ai_service.sse_listen(task_id),
        media_type="text/event-stream",
    )


@router.get("/active/{project_id}")
async def get_active_task(project_id: str, session_id: str = Query(None)):
    result = await ai_service.get_active_task(project_id, session_id)
    return ok(result)


@router.post("/cancel/{project_id}/{task_id}")
async def cancel_task(project_id: str, task_id: str):
    """Cancel a running chat task durably; returns its effective status."""
    project_service.get_project_path(project_id)
    return ok(await ai_service.cancel_task(project_id, task_id))


@router.get("/tasks/{project_id}")
async def get_tasks(project_id: str, session_id: str = Query(None)):
    tasks = await ai_service.get_tasks(project_id, session_id)
    return ok(tasks)


@router.get("/context-stats/{project_id}")
async def get_context_stats(project_id: str, session_id: str = Query(...)):
    stats = await ai_service.get_context_stats(project_id, session_id)
    return ok(stats)


@router.get("/history/{project_id}")
async def get_chat_history(
    project_id: str,
    session_id: str = Query(None),
    limit: int = Query(10, ge=1, le=200),
    before_seq: int | None = Query(None, ge=0),
):
    """Get chat history for a session. Falls back to default session for backward compat."""
    data = await ai_service.get_history(
        project_id, session_id=session_id,
        limit=limit, before_seq=before_seq,
    )
    return ok(data)


@router.post("/edit/{project_id}/{session_id}")
async def edit_chat_message(project_id: str, session_id: str, data: EditChatMessageRequest):
    """Edit a user message, delete it and later messages, then stream a new reply."""
    result = await ai_service.edit_and_submit_chat(
        project_id=project_id,
        session_id=session_id,
        message_id=data.message_id,
        message=data.message,
        context={
            "user_state": data.user_state,
            "attachments": data.attachments,
            "token_budget": data.token_budget,
        },
    )
    return StreamingResponse(
        ai_service.sse_listen(result["task_id"], project_id=project_id),
        media_type="text/event-stream",
    )


@router.delete("/history/{project_id}")
async def clear_chat_history(project_id: str, session_id: str = Query(None)):
    await ai_service.clear_history(project_id, session_id=session_id)
    return ok(None)


@router.post("/attachments/{project_id}")
async def upload_chat_attachment(project_id: str, file: UploadFile = File(...)):
    content = await file.read()
    attachment = await save_chat_image(
        project_id=project_id,
        filename=file.filename or "image",
        content=content,
        mime_type=file.content_type or "",
    )
    return ok(attachment)


# ── Session CRUD ──

@router.get("/sessions/{project_id}")
async def list_sessions(project_id: str, include_archived: bool = Query(False)):
    sessions = await ai_service.list_sessions(project_id, include_archived=include_archived)
    return ok(sessions)


@router.post("/sessions/{project_id}")
async def create_session(project_id: str):
    session = await ai_service.create_session(project_id)
    return ok(session)


@router.patch("/sessions/{project_id}/{session_id}")
async def update_session(project_id: str, session_id: str, data: UpdateSessionRequest):
    """Update session title or archive state."""
    await ai_service.update_session(project_id, session_id,
                                     title=data.title, is_archived=data.is_archived)
    return ok(None)


@router.delete("/sessions/{project_id}/{session_id}")
async def delete_session(project_id: str, session_id: str):
    """Delete a session and all its messages."""
    await ai_service.delete_session(project_id, session_id)
    return ok(None)


@router.post("/sessions/{project_id}/{session_id}/generate-title")
async def generate_session_title(project_id: str, session_id: str):
    """Generate a title for a session based on its first messages."""
    title = await ai_service.generate_title(project_id, session_id)
    return ok({"title": title})


@router.post("/sessions/{project_id}/{session_id}/load-skill")
async def load_skill_into_session(project_id: str, session_id: str, data: SkillLoadRequest):
    """Inject a completed skill_load tool turn into the session (no LLM call)."""
    result = await ai_service.load_skill_into_session(project_id, session_id, data.skill_id)
    return ok(result)


@router.get("/sessions/{project_id}/{session_id}/messages")
async def get_session_messages(project_id: str, session_id: str):
    """Get all messages for a session (for archive preview)."""
    messages = await ai_service.get_session_messages(project_id, session_id)
    return ok(messages)
