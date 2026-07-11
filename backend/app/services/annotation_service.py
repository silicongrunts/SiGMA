"""
Annotation Service — business logic for file annotations.

Thread replies are stored as Message rows (annotation_id FK)
via UnitOfWork → message_repo.
"""

from typing import Any, Dict, List

from app.core.logging import get_logger
from app.core.message_format import (
    build_assistant_turn,
    build_tool_results_index,
    finalize_assistant_turn,
)
from app.core.utils import generate_id, to_iso
from app.core.task_status import (
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLING,
    STATUS_QUEUED,
    STATUS_RUNNING,
)
from app.database.unit_of_work import UnitOfWork
from app.services.file_service import file_service

logger = get_logger(__name__)


def serialize_annotation(annotation) -> Dict[str, Any]:
    """Serialize an Annotation ORM object into the full UI dict with thread.

    This is the **only** place that constructs the annotation thread view for
    the frontend.  The ORM model's ``to_dict()`` is limited to raw field
    serialization — all UI aggregation lives here.

    Args:
        annotation: An ``Annotation`` ORM instance with eagerly loaded
            ``messages`` relationship.

    Returns:
        ``{"id", "from", "to", "originalText", "thread": [...]}``
    """
    messages = annotation.messages
    tool_results = build_tool_results_index(messages)

    thread: List[Dict[str, Any]] = []
    i = 0
    while i < len(messages):
        m = messages[i]

        if m.role == "tool":
            i += 1
            continue

        if m.role == "system":
            i += 1
            continue

        if m.role == "assistant":
            turn, next_i = build_assistant_turn(messages, i, tool_results)
            finalized = finalize_assistant_turn(turn)

            entry: Dict[str, Any] = {
                "role": "SiGMA",
                "content": finalized["text"],
                "created_at": to_iso(m.created_at),
            }
            if "process" in finalized:
                entry["process"] = finalized["process"]
            if finalized.get("token_count"):
                entry["token_count"] = finalized["token_count"]
            if finalized.get("cached_tokens"):
                entry["cached_tokens"] = finalized["cached_tokens"]
            if finalized.get("input_tokens"):
                entry["input_tokens"] = finalized["input_tokens"]
            thread.append(entry)
            i = next_i

        elif m.role == "user":
            thread.append({
                "role": "user",
                "content": m.content,
                "created_at": to_iso(m.created_at),
            })
            i += 1

        else:
            i += 1

    return {
        "id": annotation.id,
        "from": annotation.from_pos,
        "to": annotation.to_pos,
        "originalText": annotation.original_text,
        "thread": thread,
    }


class AnnotationService:
    """Service-layer CRUD for annotations, delegating DB work to repositories."""

    # -- public API ------------------------------------------------------------

    @staticmethod
    def _is_persistable_annotation(annotation: Dict) -> bool:
        from_pos = annotation.get("from", 0)
        to_pos = annotation.get("to", 0)
        original_text = annotation.get("originalText") or ""
        return to_pos > from_pos and bool(original_text.strip())

    async def get_annotations(
        self,
        project_id: str,
        file_path: str,
    ) -> List[Dict]:
        """Return all annotations for *file_path* as plain dicts."""
        async with UnitOfWork(project_id) as uow:
            annotations = await uow.annotations.get_by_file(file_path)
            return [serialize_annotation(a) for a in annotations]

    async def add_annotation(
        self,
        project_id: str,
        file_path: str,
        from_pos: int,
        to_pos: int,
        text: str,
        role: str = "assistant",
    ) -> Dict:
        """Create a new annotation with an initial reply."""
        # Read original text from the file via file_service
        try:
            file_content = await file_service.read_file(project_id, file_path)
            original_text = file_content[from_pos:to_pos]
        except Exception:
            logger.warning("Failed to read annotation source text from %s; original_text snapshot will be empty", file_path, exc_info=True)
            original_text = ""

        anno_id = generate_id()

        async with UnitOfWork(project_id) as uow:
            annotation = await uow.annotations.create(
                file_path=file_path,
                from_pos=from_pos,
                to_pos=to_pos,
                original_text=original_text,
                annotation_id=anno_id,
            )
            await uow.messages.create_for_annotation(
                annotation_id=annotation.id,
                role=role,
                content=text,
            )

            # Re-fetch to include the message in the returned dict
            refreshed = await uow.annotations.get_by_id(annotation.id)
            return serialize_annotation(refreshed) if refreshed else {
                "id": anno_id,
                "from": from_pos,
                "to": to_pos,
                "originalText": original_text,
                "success": True,
            }

    async def save_annotations(
        self,
        project_id: str,
        file_path: str,
        annotations: List[Dict],
    ) -> Dict:
        """Replace all annotations for *file_path* with the provided list."""
        async with UnitOfWork(project_id) as uow:
            existing_annotations = await uow.annotations.get_by_file(file_path)
            existing_by_id = {annotation.id: annotation for annotation in existing_annotations}

            # ``existing_by_id`` lets orphan placeholders keep their stored
            # anchors while still participating in replace-style saves.
            persistable_annotations = []
            for annotation in annotations:
                annotation_to_save = annotation
                if annotation.get("status") == "orphan":
                    existing_annotation = existing_by_id.get(annotation.get("id"))
                    if existing_annotation:
                        annotation_to_save = {
                            **annotation,
                            "from": existing_annotation.from_pos,
                            "to": existing_annotation.to_pos,
                            "originalText": existing_annotation.original_text,
                        }
                if self._is_persistable_annotation(annotation_to_save):
                    persistable_annotations.append(annotation_to_save)

            # save_all only manages annotation rows (CASCADE deletes old messages)
            await uow.annotations.save_all(file_path, persistable_annotations)

            # Create initial messages only for newly persisted draft annotations.
            # Existing annotation threads are append-only and must not be rebuilt
            # during position sync, especially while an AI reply is streaming.
            for anno_data in persistable_annotations:
                anno_id = anno_data.get("id")
                if not anno_id or anno_id in existing_by_id:
                    continue
                for reply_data in anno_data.get("thread", []):
                    role = reply_data.get("role", "user")
                    content = reply_data.get("content", "")
                    if not content.strip():
                        continue
                    await uow.messages.create_for_annotation(
                        annotation_id=anno_id,
                        role=role,
                        content=content,
                    )
        return {"success": True}

    async def reply_annotation(
        self,
        project_id: str,
        file_path: str,
        anno_id: str,
        content: str,
        role: str = "assistant",
    ) -> Dict:
        """Append a reply to an existing annotation."""
        if not content.strip():
            return {"error": "Annotation reply content cannot be empty.", "success": False}

        async with UnitOfWork(project_id) as uow:
            if file_path:
                annotation = await uow.annotations.get_annotation(
                    file_path, anno_id
                )
            else:
                annotation = await uow.annotations.get_by_id(anno_id)
            if not annotation:
                return {"error": f"Annotation {anno_id} not found.", "success": False}

            await uow.messages.create_for_annotation(
                annotation_id=anno_id,
                role=role,
                content=content,
            )
        return {"success": True, "anno_id": anno_id}

    async def get_active_reply_task(
        self, project_id: str, annotation_id: str
    ) -> Dict:
        """Return active AI reply task state for one annotation."""
        try:
            async with UnitOfWork(project_id) as uow:
                active = await uow.task_state.get_active_annotation_reply(annotation_id)
                if not active:
                    return {"active": False, "task_id": None, "status": None}
                liveness = await uow.task_state.check_liveness(active["task_id"])
            if liveness == "stale":
                return {
                    "active": False,
                    "task_id": active["task_id"],
                    "status": "stale",
                    "task_type": active.get("task_type"),
                    "annotation_id": annotation_id,
                    "recoverable": True,
                    "message": (
                        "The previous annotation task stopped sending heartbeats "
                        "before it produced a final response."
                    ),
                }

            return {
                "active": liveness in (
                    STATUS_QUEUED, STATUS_RUNNING,
                    STATUS_AWAITING_INPUT, STATUS_CANCELLING,
                ),
                "task_id": active["task_id"],
                "status": liveness,
                "task_type": active.get("task_type"),
                "annotation_id": annotation_id,
            }
        except Exception:
            logger.debug("Failed to read active annotation task %s", annotation_id, exc_info=True)
            return {"active": False, "task_id": None, "status": None}

    async def resolve_annotation(
        self, project_id: str, annotation_id: str
    ) -> tuple:
        """Resolve an annotation by exact ID.

        Returns (annotation_orm, error_message). One of them is None.
        """
        async with UnitOfWork(project_id) as uow:
            return await uow.annotations.resolve(annotation_id)

    async def list_annotations_by_file(
        self, project_id: str, file_path: str
    ) -> list:
        """Return all annotations for a file as ORM objects."""
        async with UnitOfWork(project_id) as uow:
            return await uow.annotations.get_by_file(file_path)

    async def delete_annotation(
        self, project_id: str, annotation_id: str
    ) -> tuple:
        """Delete an annotation by exact ID.

        Returns (success: bool, error_message: str|None).
        """
        async with UnitOfWork(project_id) as uow:
            return await uow.annotations.delete_by_id(annotation_id)

    async def start_ai_reply_stream(
        self,
        project_id: str,
        file_path: str,
        annotation_id: str,
    ) -> tuple[str, "AsyncGenerator"]:
        """Submit an AI reply task and return (task_id, SSE async generator).

        The caller (route handler) wraps the generator in a
        ``StreamingResponse`` — the service layer stays free of HTTP
        framework imports.
        """
        from app.services.ai_service import ai_service
        from app.workers.huey_tasks import run_annotation_reply

        task_id = generate_id()
        async with UnitOfWork(project_id) as uow:
            await uow.task_state.set_queued(
                task_id,
                task_type="annotation_reply",
                owner_type="annotation",
                owner_id=annotation_id,
            )

        run_annotation_reply(
            task_id=task_id,
            project_id=project_id,
            file_path=file_path,
            annotation_id=annotation_id,
        )

        return task_id, ai_service.sse_listen(task_id, project_id=project_id)


# Singleton instance
annotation_service = AnnotationService()
