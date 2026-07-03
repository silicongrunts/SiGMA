"""
Index Builder Service - RAG index builder.

Each document is indexed individually via the durable background task queue.
Crash recovery is handled by task leases and the periodic queue scanner.

Responsibilities:
- Process a single document: load content → chunk → embed → store in ChromaDB
- Mark documents as "completed" (success) or "failed" (permanent error)
- Support cancellation via cancel_event (TCP) or DB "cancelling" status
- Report progress between embedding batches via progress_callback
"""
import asyncio
from typing import Optional

from app.core.document_status import STATUS_CANCELLING, STATUS_COMPLETED
from app.core.utils import utcnow
from app.database.unit_of_work import UnitOfWork

from app.core.logging import get_logger
logger = get_logger(__name__)

# Error substrings that indicate permanent (non-retryable) failures
_PERMANENT_ERROR_MARKERS = (
    "expecting embedding with dimension",
    "embedding dimension mismatch",
)


class IndexBuilderService:
    """RAG index builder with cancellation and stale-revision checks."""

    def __init__(self):
        self._running: bool = False

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Mark the builder as running. Call at backend startup (non-async)."""
        self._running = True
        logger.info("Index builder started")

    def stop(self):
        """Stop the builder. Call at backend shutdown."""
        self._running = False
        logger.info("Index builder stopped")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _doc_gone_or_stale(
        self,
        project_id: str,
        doc_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        """Check if document was deleted, cancelled, or superseded."""
        try:
            async with UnitOfWork(project_id) as uow:
                doc = await uow.library.get_by_id(doc_id)
            if not doc or doc.processing_status == STATUS_CANCELLING:
                return True
            return expected_revision is not None and doc.revision != expected_revision
        except Exception:
            logger.warning("Failed to check document stale state for %s; treating as gone", doc_id, exc_info=True)
            return True

    async def _mark_doc_failed(self, project_id: str, doc_id: str, log_msg: str):
        """Mark a document as failed in the database."""
        try:
            async with UnitOfWork(project_id) as uow:
                await uow.library.mark_failed(doc_id, log_msg)
        except Exception as e:
            logger.error("Failed to mark doc %s as failed: %s", doc_id, e, exc_info=True)

    # ------------------------------------------------------------------
    # Process a single document
    # ------------------------------------------------------------------

    async def process_one(self, project_id: str, doc_id: str,
                          cancel_event: Optional[asyncio.Event] = None,
                          expected_revision: int | None = None,
                          task_context=None) -> bool:
        """Index a single document for RAG.

        Returns True on success or permanent failure (caller should not retry).
        Returns False on transient failure (periodic scanner will retry).
        """
        try:
            # Check cancel at start
            if cancel_event and cancel_event.is_set():
                return True
            if task_context and await task_context.is_cancelling():
                return True

            # 1. Load the document
            async with UnitOfWork(project_id) as uow:
                doc = await uow.library.get_by_id(doc_id)

            if not doc:
                logger.debug("Document %s not found, skipping", doc_id)
                return True
            if expected_revision is not None and doc.revision != expected_revision:
                logger.info("Document %s revision changed, skipping stale index task", doc_id)
                return True

            # 2. If no content, nothing to index — mark completed
            if not doc.content or not doc.content.strip():
                logger.info("Document %s has no content, marking completed", doc_id)
                async with UnitOfWork(project_id) as uow:
                    await uow.library.update_processing_status(
                        doc_id, STATUS_COMPLETED, completed_at=utcnow(),
                    )
                return True

            # 3. Create progress callback for task heartbeat between embedding batches
            loop = asyncio.get_running_loop()
            progress_cb = None
            if task_context:
                def progress_cb():
                    future = asyncio.run_coroutine_threadsafe(
                        task_context.heartbeat(), loop
                    )
                    try:
                        future.result(timeout=5.0)
                    except Exception as e:
                        logger.warning("Task heartbeat failed for %s: %s", doc_id, e, exc_info=True)

            def should_continue() -> bool:
                future = asyncio.run_coroutine_threadsafe(
                    self._doc_gone_or_stale(project_id, doc_id, expected_revision),
                    loop,
                )
                try:
                    return not future.result(timeout=5.0)
                except Exception as e:
                    logger.warning("Stale check failed for %s: %s", doc_id, e, exc_info=True)
                    return False

            # 4. Index in RAG
            from app.services.rag_service import rag_service
            await rag_service.index_document(
                project_id, doc_id, doc.content,
                title=doc.title, description=doc.description or "",
                progress_callback=progress_cb,
                cancel_event=cancel_event,
                should_continue=should_continue,
                doc_revision=expected_revision,
            )
            logger.info("RAG indexing completed for %s in project %s", doc_id, project_id)

            if await self._doc_gone_or_stale(project_id, doc_id, expected_revision):
                logger.info("Document %s changed during indexing, skipping completion", doc_id)
                return True

            # 5. Post-cancel cleanup: if cancelled during indexing, remove orphaned chunks
            if cancel_event and cancel_event.is_set():
                logger.info("Document %s cancelled during indexing, removing chunks", doc_id)
                await rag_service.remove_document(project_id, doc_id)
                return True
            if task_context and await task_context.is_cancelling():
                logger.info("Document %s task cancelled during indexing, removing chunks", doc_id)
                await rag_service.remove_document(project_id, doc_id)
                return True

            # 6. Post-indexing check: if doc was deleted during indexing, clean up
            async with UnitOfWork(project_id) as uow:
                doc_check = await uow.library.get_by_id(doc_id)
            if not doc_check:
                logger.info("Document %s deleted during indexing, removing chunks", doc_id)
                await rag_service.remove_document(project_id, doc_id)
                return True
            if doc_check.processing_status == STATUS_CANCELLING:
                logger.info("Document %s is cancelling, removing chunks", doc_id)
                await rag_service.remove_document(project_id, doc_id)
                return True

            # 7. Mark completed
            async with UnitOfWork(project_id) as uow:
                await uow.library.update_processing_status(
                    doc_id, STATUS_COMPLETED, completed_at=utcnow(),
                )

            return True

        except Exception as e:
            error_msg = str(e)
            logger.error("Failed to index %s in project %s: %s", doc_id, project_id, e, exc_info=True)

            # If doc was deleted/cancelled, don't retry
            if await self._doc_gone_or_stale(project_id, doc_id, expected_revision):
                return True

            # Permanent error — no retry
            error_lower = error_msg.lower()
            if any(marker in error_lower for marker in _PERMANENT_ERROR_MARKERS):
                await self._mark_doc_failed(
                    project_id, doc_id,
                    f"Embedding dimension mismatch. Please rebuild index. Error: {e}",
                )
                return True

            # Transient — durable background task retry owns the retry budget.
            return False


# Global singleton
index_builder = IndexBuilderService()


# ---------------------------------------------------------------------------
# Background-task handler registration
# ---------------------------------------------------------------------------
#
# Register the RAG-index handler with the library task protocol at module
# load.  ``services.background_task_service`` dispatches via the registry
# rather than importing this module, which keeps the dependency graph
# acyclic.

async def _handle_rag_index_task(ctx, payload: dict) -> None:
    """Run one RAG-indexing task on the library queue."""
    if not index_builder.is_running():
        index_builder.start()
    completed = await index_builder.process_one(
        ctx.project_id,
        payload["doc_id"],
        expected_revision=payload.get("doc_revision"),
        task_context=ctx,
    )
    if not completed:
        raise RuntimeError("Transient RAG indexing failure")


def _register_library_handler() -> None:
    from app.services.library_task_protocol import (
        KIND_RAG_INDEX, register_task_handler,
    )
    register_task_handler(KIND_RAG_INDEX, _handle_rag_index_task)


_register_library_handler()
