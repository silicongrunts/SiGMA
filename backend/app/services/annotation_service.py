"""
Annotation Service — business logic for file annotations.

Thread replies are stored as Message rows (annotation_id FK)
via UnitOfWork → message_repo.
"""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, List

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AnnotationConflictError, TaskActiveError, ValidationError
from app.core.logging import get_logger
from app.core.message_format import (
    build_assistant_turn,
    build_tool_results_index,
    finalize_assistant_turn,
)
from app.core.utils import generate_id, to_iso
from app.database.unit_of_work import UnitOfWork
from app.services.file_service import file_service
from app.services.project_service import project_service

logger = get_logger(__name__)


def _text_content(raw) -> str:
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


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
    ) -> Dict[str, Any]:
        """Return a published annotation snapshot for *file_path*."""
        await self.recover_transactions(project_id)
        async with UnitOfWork(project_id, immediate=True) as uow:
            annotations = await uow.annotations.get_by_file(file_path)
            file_content = await file_service.read_file(project_id, file_path)
            file_hash = file_service.compute_hash(_text_content(file_content))
            state = await uow.annotations.ensure_file_state(file_path, file_hash)
            if state.file_hash != file_hash:
                state.file_hash = file_hash
                state.revision += 1
            serialized = [serialize_annotation(a) for a in annotations]
            await uow.commit()
            return {
                "annotations": serialized,
                "revision": state.revision,
                "fileHash": state.file_hash,
            }

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
        anno_id = generate_id()
        async with UnitOfWork(project_id, immediate=True) as uow:
            file_content = _text_content(await file_service.read_file(project_id, file_path))
            original_text = file_content[from_pos:to_pos]
            if from_pos < 0 or to_pos <= from_pos or not original_text.strip():
                raise ValidationError("Annotation range must contain non-blank file text")
            file_hash = file_service.compute_hash(file_content)
            state = await uow.annotations.ensure_file_state(file_path, file_hash)
            verification = _text_content(await file_service.read_file(project_id, file_path))
            verified_hash = file_service.compute_hash(verification)
            if verified_hash != file_hash:
                raise AnnotationConflictError(
                    details={"revision": state.revision, "fileHash": verified_hash},
                )
            await uow.annotations.apply_mutation_cas(
                file_path, [{"id": anno_id, "from": from_pos, "to": to_pos,
                             "originalText": original_text,
                             "thread": [{"role": role, "content": text}]}], [],
                state.revision, file_hash,
            )
            refreshed = await uow.annotations.get_by_id(anno_id)
            created = serialize_annotation(refreshed)

        project_service.touch_project(project_id)
        return created

    async def save_annotations(
        self,
        project_id: str,
        file_path: str,
        annotations: List[Dict],
        expected_revision: int | None = None,
        expected_file_hash: str | None = None,
        delete_ids: List[str] | None = None,
    ) -> Dict:
        """Apply explicit anchor updates; never infer deletion from omission."""
        await self.recover_transactions(project_id)
        if expected_revision is None or expected_file_hash is None:
            snapshot = await self.get_annotations(project_id, file_path)
            expected_revision = snapshot["revision"]
            expected_file_hash = snapshot["fileHash"]
        await self._drain_annotation_tasks(project_id, delete_ids or [])
        file_content = await file_service.read_file(project_id, file_path)
        current_hash = file_service.compute_hash(_text_content(file_content))
        async with UnitOfWork(project_id, immediate=True) as uow:
            state = await uow.annotations.ensure_file_state(file_path, current_hash)
            revision = expected_revision
            file_hash = expected_file_hash
            if current_hash != file_hash or state.revision != revision:
                raise AnnotationConflictError(details={
                    "revision": state.revision,
                    "fileHash": current_hash,
                })
            try:
                next_state = await uow.annotations.apply_mutation_cas(
                    file_path, annotations, delete_ids or [], revision, file_hash,
                )
            except ValueError as exc:
                raise AnnotationConflictError() from exc
        project_service.touch_project(project_id)
        await self._cleanup_annotation_task_state(project_id, delete_ids or [])
        return {"success": True, "revision": next_state.revision, "fileHash": current_hash}

    async def save_document(
        self, project_id: str, file_path: str, content: str,
        expected_file_hash: str, expected_revision: int, annotations: List[Dict],
        delete_ids: List[str] | None = None,
    ) -> Dict:
        """Commit a file and its anchor updates through a durable journal."""
        await self.recover_transactions(project_id)
        await self._drain_annotation_tasks(project_id, delete_ids or [])
        old_bytes = await file_service.read_file(project_id, file_path)
        old_content = _text_content(old_bytes)
        current_hash = file_service.compute_hash(old_content)
        if current_hash != expected_file_hash:
            raise AnnotationConflictError(details={"revision": expected_revision, "fileHash": current_hash})
        new_hash = file_service.compute_hash(content)
        transaction_id = generate_id()
        mutations = {"upserts": annotations, "deleteIds": delete_ids or []}
        async with UnitOfWork(project_id, immediate=True) as uow:
            state = await uow.annotations.get_file_state(file_path)
            if state is None:
                state = await uow.annotations.ensure_file_state(file_path, expected_file_hash)
            if state.revision != expected_revision or state.file_hash != expected_file_hash:
                raise AnnotationConflictError(details={"revision": state.revision if state else 0, "fileHash": current_hash})
            await uow.annotations.create_transaction(
                id=transaction_id, file_path=file_path,
                expected_revision=expected_revision, expected_file_hash=expected_file_hash,
                new_file_hash=new_hash, old_content=old_content,
                mutations=json.dumps(mutations),
            )
        async with UnitOfWork(project_id, immediate=True) as uow:
            await uow.file_deletions.assert_available(file_path)
            if await uow.annotations.get_transaction(transaction_id) is None:
                raise AnnotationConflictError()
            write_result = file_service.write_file_content(
                project_id, file_path, content, expected_hash=expected_file_hash,
                require_expected_hash=True,
            )
        if write_result.get("conflict"):
            async with UnitOfWork(project_id, immediate=True) as uow:
                await uow.annotations.delete_transaction(transaction_id)
            raise AnnotationConflictError(details={"revision": expected_revision, "fileHash": current_hash})
        try:
            async with UnitOfWork(project_id, immediate=True) as uow:
                next_state = await uow.annotations.apply_mutation_cas(
                    file_path, annotations, delete_ids or [], expected_revision,
                    expected_file_hash, resulting_file_hash=new_hash,
                )
                await uow.annotations.delete_transaction(transaction_id)
        except ValueError as exc:
            await self.recover_transactions(project_id)
            raise AnnotationConflictError(
                details={"revision": expected_revision, "fileHash": new_hash},
            ) from exc
        project_service.touch_project(project_id)
        await self._cleanup_annotation_task_state(project_id, delete_ids or [])
        from app.services.snapshot_service import snapshot_service
        await snapshot_service.maybe_snapshot(project_id)
        return {"success": True, "revision": next_state.revision, "fileHash": new_hash}

    async def _drain_annotation_tasks(
        self, project_id: str, annotation_ids: List[str],
    ) -> None:
        from app.services import task_runtime

        for annotation_id in dict.fromkeys(annotation_ids):
            async with UnitOfWork(project_id, immediate=True) as uow:
                active = await uow.task_state.get_active_by_owner(
                    "annotation", annotation_id,
                )
                task_id = active["task_id"] if active else None
                if task_id:
                    status = await uow.task_state.request_cancel(task_id)
                    if status == "awaiting_input":
                        await uow.task_state.mark_cancelled(task_id)
            if not task_id:
                continue
            task_runtime.cancel(task_id)
            if not await task_runtime.wait_for_task(task_id):
                raise TaskActiveError(task_id=task_id)

    async def _cleanup_annotation_task_state(
        self, project_id: str, annotation_ids: List[str],
    ) -> None:
        await self._drain_annotation_tasks(project_id, annotation_ids)
        for annotation_id in dict.fromkeys(annotation_ids):
            async with UnitOfWork(project_id) as uow:
                await uow.task_state.delete_by_owner("annotation", annotation_id)

    async def _restore_transaction_file(
        self, project_id: str, transaction,
    ) -> None:
        """Restore a failed save only while the journal version is on disk."""
        file_service.write_file_content(
            project_id,
            transaction.file_path,
            transaction.old_content,
            expected_hash=transaction.new_file_hash,
            require_expected_hash=True,
        )

    async def recover_transactions(self, project_id: str) -> int:
        """Recover journaled saves without replacing an unrelated disk version."""
        recovered = 0
        async with UnitOfWork(project_id) as uow:
            transactions = await uow.annotations.get_transactions()
        for transaction in transactions:
            async with UnitOfWork(project_id, immediate=True) as uow:
                if await uow.annotations.get_transaction(transaction.id) is None:
                    continue
                if await uow.file_deletions.is_deleting(transaction.file_path):
                    continue
                raw = await file_service.read_file(project_id, transaction.file_path)
                disk_content = _text_content(raw)
                disk_hash = file_service.compute_hash(disk_content)
                mutations = json.loads(transaction.mutations)
                state = await uow.annotations.get_file_state(transaction.file_path)
                if disk_hash == transaction.expected_file_hash:
                    await uow.annotations.delete_transaction(transaction.id)
                    continue
                if state and (
                    state.revision == transaction.expected_revision + 1
                    and state.file_hash == transaction.new_file_hash
                ):
                    await uow.annotations.delete_transaction(transaction.id)
                    continue
                if state and (
                    state.revision == transaction.expected_revision
                    and state.file_hash == transaction.expected_file_hash
                    and disk_hash == transaction.new_file_hash
                ):
                    try:
                        await uow.annotations.apply_mutation_cas(
                            transaction.file_path, mutations.get("upserts", []),
                            mutations.get("deleteIds", []), transaction.expected_revision,
                            transaction.expected_file_hash, transaction.new_file_hash,
                        )
                    except ValueError:
                        continue
                    await uow.annotations.delete_transaction(transaction.id)
                    recovered += 1
                    continue
                if disk_hash != transaction.new_file_hash:
                    await uow.annotations.delete_transaction(transaction.id)
                    continue
            if disk_hash == transaction.new_file_hash:
                async with UnitOfWork(project_id, immediate=True) as uow:
                    if await uow.annotations.get_transaction(transaction.id) is None:
                        continue
                    if await uow.file_deletions.is_deleting(transaction.file_path):
                        continue
                    state = await uow.annotations.get_file_state(transaction.file_path)
                    is_other_mutation = state and (
                        state.revision > transaction.expected_revision
                        and state.file_hash == transaction.expected_file_hash
                    )
                    if not is_other_mutation:
                        continue
                    await self._restore_transaction_file(project_id, transaction)
                    try:
                        restored = _text_content(
                            await file_service.read_file(project_id, transaction.file_path)
                        )
                    except Exception:
                        continue
                    restored_hash = file_service.compute_hash(restored)
                    if restored_hash == transaction.new_file_hash:
                        continue
                    await uow.annotations.delete_transaction(transaction.id)
            # A third-party disk version, or a failed guarded restore, keeps
            # the journal as recovery evidence for the next access/startup.
        return recovered

    async def reply_annotation(
        self,
        project_id: str,
        file_path: str,
        anno_id: str,
        content: str,
        role: str = "assistant",
    ) -> Dict:
        """Append one message without changing the annotation anchor snapshot."""
        if not content.strip():
            return {"error": "Annotation reply content cannot be empty.", "success": False}

        resolved, error = await self.resolve_annotation(project_id, anno_id)
        if not resolved or (file_path and resolved.file_path != file_path):
            return {"error": error or f"Annotation {anno_id} not found.", "success": False}
        await self.get_annotations(project_id, resolved.file_path)
        async with UnitOfWork(project_id, immediate=True) as uow:
            annotation = await uow.annotations.get_by_id(anno_id)
            if not annotation or (file_path and annotation.file_path != file_path):
                return {"error": f"Annotation {anno_id} not found.", "success": False}
            raw = await file_service.read_file(project_id, annotation.file_path)
            file_hash = file_service.compute_hash(_text_content(raw))
            state = await uow.annotations.ensure_file_state(annotation.file_path, file_hash)
            if state.file_hash != file_hash:
                raise AnnotationConflictError(
                    details={"revision": state.revision, "fileHash": file_hash},
                )
            await uow.messages.stage_create_for_annotation(
                annotation_id=anno_id,
                role=role,
                content=content,
            )
            await uow.commit()
        project_service.touch_project(project_id)
        return {
            "success": True, "anno_id": anno_id,
            "revision": state.revision, "fileHash": state.file_hash,
        }

    async def get_active_reply_task(
        self, project_id: str, annotation_id: str
    ) -> Dict:
        """Return active AI reply task state for one annotation."""
        try:
            async with UnitOfWork(project_id) as uow:
                active = await uow.task_state.get_active_annotation_reply(annotation_id)
                if not active:
                    return {"active": False, "task_id": None, "status": None}
            return {
                "active": True,
                "task_id": active["task_id"],
                "status": active["status"],
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
            resolved, error = await uow.annotations.resolve(annotation_id)
        if error is None:
            project_service.touch_project(project_id)
        return resolved, error

    async def list_annotations_by_file(
        self, project_id: str, file_path: str
    ) -> list:
        """Return all annotations for a file as ORM objects."""
        async with UnitOfWork(project_id) as uow:
            return await uow.annotations.get_by_file(file_path)

    async def delete_annotation(
        self, project_id: str, annotation_id: str,
        expected_revision: int | None = None,
        expected_file_hash: str | None = None,
    ) -> Dict[str, Any]:
        """Delete one annotation and return the committed CAS snapshot."""
        await self.recover_transactions(project_id)
        if expected_revision is None and expected_file_hash is None:
            annotation = await self.resolve_annotation(project_id, annotation_id)
            if annotation[0] is None:
                return {"deleted": False, "error": annotation[1]}
            snapshot = await self.get_annotations(project_id, annotation[0].file_path)
            expected_revision = snapshot["revision"]
            expected_file_hash = snapshot["fileHash"]
        await self._drain_annotation_tasks(project_id, [annotation_id])

        async with UnitOfWork(project_id, immediate=True) as uow:
            annotation = await uow.annotations.get_by_id(annotation_id)
            if not annotation:
                return {
                    "deleted": False,
                    "error": f"No annotation found with ID '{annotation_id}'",
                }
            file_content = await file_service.read_file(project_id, annotation.file_path)
            current_hash = file_service.compute_hash(_text_content(file_content))
            state = await uow.annotations.ensure_file_state(annotation.file_path, current_hash)
            if expected_revision is not None and state.revision != expected_revision:
                raise AnnotationConflictError(details={"revision": state.revision, "fileHash": current_hash})
            if expected_file_hash is not None and current_hash != expected_file_hash:
                raise AnnotationConflictError(details={"revision": state.revision, "fileHash": current_hash})
            try:
                next_state = await uow.annotations.apply_mutation_cas(
                    annotation.file_path, [], [annotation_id], state.revision,
                    current_hash,
                )
            except ValueError as exc:
                raise AnnotationConflictError(
                    details={"revision": state.revision, "fileHash": current_hash},
                ) from exc
        await self._cleanup_annotation_task_state(project_id, [annotation_id])
        from app.agents.tools.read_state import read_state_cache
        read_state_cache.clear(f"annotation:{annotation_id}")
        project_service.touch_project(project_id)
        return {
            "deleted": True,
            "error": None,
            "revision": next_state.revision,
            "fileHash": next_state.file_hash,
        }

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
        from app.services.annotation_loop import AnnotationLoop
        from app.services import task_runtime

        if not project_service.is_project_active(project_id):
            raise ValidationError("Project is unavailable")

        task_id = generate_id()
        try:
            async with UnitOfWork(project_id, immediate=True) as uow:
                annotation = await uow.annotations.get_by_id(annotation_id)
                if annotation is None:
                    raise ValidationError("Annotation no longer exists")
                await uow.file_deletions.assert_available(annotation.file_path)
                await uow.task_state.set_queued(
                    task_id,
                    task_type="annotation_reply",
                    owner_type="annotation",
                    owner_id=annotation_id,
                )
        except IntegrityError:
            # The partial unique index over runnable rows per owner rejects a
            # concurrent second submit; surface the same user-facing error the
            # chat path raises instead of an unhandled 500.
            async with UnitOfWork(project_id) as uow:
                existing = await uow.task_state.get_active_annotation_reply(
                    annotation_id
                )
            raise TaskActiveError(
                task_id=existing["task_id"] if existing else "",
            )

        try:
            async with UnitOfWork(project_id) as uow:
                await uow.task_state.prune_terminal_by_owner("annotation", annotation_id)
        except Exception:
            logger.debug(
                "Terminal-row prune failed for annotation %s",
                annotation_id,
                exc_info=True,
            )

        try:
            task_runtime.launch(
                task_id=task_id,
                project_id=project_id,
                source_factory=lambda cancel_event: AnnotationLoop(
                    project_id=project_id,
                    file_path=file_path,
                    annotation_id=annotation_id,
                    cancel_event=cancel_event,
                ).run(),
            )
        except BaseException as e:
            # The queued row was committed above and now owns the annotation:
            # a failure between the claim and the runner launch — including a
            # CancelledError from a disconnecting client, which ``except
            # Exception`` would let through — must finalize the row before
            # propagating, or it would strand until the stranded-row sweep.
            if isinstance(e, asyncio.CancelledError):
                error = "Task was cancelled before it started."
            else:
                error = f"Failed to start annotation task: {e}"
            try:
                async with UnitOfWork(project_id) as uow:
                    await uow.task_state.mark_failed(task_id, error)
            except Exception:
                # Best-effort cleanup: the original launch failure must not
                # be masked by a failure to finalize the task row.
                logger.warning(
                    "Failed to finalize annotation task %s after launch error",
                    task_id, exc_info=True,
                )
            raise

        return task_id, ai_service.sse_listen(task_id, project_id=project_id)


# Singleton instance
annotation_service = AnnotationService()
