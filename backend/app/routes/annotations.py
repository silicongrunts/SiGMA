from fastapi import APIRouter, Body, Query
from fastapi.responses import StreamingResponse
from app.services.annotation_service import annotation_service
from app.services.ai_service import ai_service
from app.services.project_service import project_service
from app.models.requests import CreateAnnotationRequest, SaveAnnotationsRequest, AnnotationStreamRequest, AnnotationReplyRequest
from app.core.utils import generate_id
from app.core.response import ok
from app.core.exceptions import ValidationError

router = APIRouter(prefix="/annotations", tags=["annotations"])

@router.get("/{project_id}")
async def get_annotations(project_id: str, path: str = Query(...)):
    """Load annotations for a specific file."""
    result = await annotation_service.get_annotations(project_id, path)
    return ok(result)

@router.post("/{project_id}/create")
async def create_annotation(project_id: str, path: str = Query(...), data: CreateAnnotationRequest = Body(...)):
    """Backend assigned ID for new annotation."""
    project_service.get_project_path(project_id)
    request_path = data.file_path or path
    if request_path != path:
        raise ValidationError("Annotation file path does not match the request path")
    return ok({"id": generate_id()})

@router.post("/{project_id}")
async def save_annotations(project_id: str, path: str = Query(...), data: SaveAnnotationsRequest = Body(...)):
    """Save all annotations for a file."""
    result = await annotation_service.save_annotations(project_id, path, data.annotations)
    return ok(result)


@router.post("/{project_id}/reply")
async def reply_annotation(project_id: str, data: AnnotationReplyRequest):
    """Append a user reply to an annotation (does NOT wipe existing messages)."""
    result = await annotation_service.reply_annotation(
        project_id=project_id,
        file_path="",
        anno_id=data.annotation_id,
        content=data.content,
        role="user",
    )
    return ok(result)


@router.post("/stream/{project_id}")
async def stream_annotation_reply(project_id: str, data: AnnotationStreamRequest):
    """Stream an AI reply for an annotation with tool support."""
    _, sse_gen = await annotation_service.start_ai_reply_stream(
        project_id=project_id,
        file_path=data.file_path,
        annotation_id=data.annotation_id,
    )
    return StreamingResponse(sse_gen, media_type="text/event-stream")


@router.get("/active/{project_id}")
async def get_active_annotation_reply(project_id: str, annotation_id: str = Query(...)):
    """Return active AI reply task for an annotation, if any."""
    result = await annotation_service.get_active_reply_task(project_id, annotation_id)
    return ok(result)


@router.get("/stream/{task_id}")
async def resume_annotation_stream(task_id: str):
    """Reconnect to an existing annotation reply SSE stream."""
    return StreamingResponse(
        ai_service.sse_listen(task_id),
        media_type="text/event-stream",
    )


@router.post("/cancel/{project_id}/{task_id}")
async def cancel_annotation_reply(project_id: str, task_id: str):
    """Cancel a running annotation reply task."""
    project_service.get_project_path(project_id)
    await ai_service.cancel_task(task_id)
    return ok({"cancelled": True})
