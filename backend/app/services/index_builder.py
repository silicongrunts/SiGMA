"""
Index Builder Service - RAG index builder.

Each document is indexed individually via the durable background task queue.
Crash recovery is handled by task leases and the periodic queue scanner.

Responsibilities:
- Process a single document: load content → chunk → embed → store in ChromaDB
- Mark documents as "completed" (success) or "failed" (permanent error)
- Stop cleanly when a document is cancelled, deleted, or superseded by a
  newer revision
- Report progress between embedding batches via progress_callback
"""
import asyncio

from app.core.document_status import STATUS_CANCELLING, STATUS_COMPLETED
from app.core.utils import is_blank_content, utcnow
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _doc_gone_or_stale(
        self,
        project_id: str,
        doc_id: str,
        expected_revision: int | None = None,
    ) -> bool:
        """Check if document was deleted, cancelled, or superseded.

        Raises on database failure so callers can distinguish "stop because
        the document is gone" from "state unknown" — the latter must surface
        as a task failure, never as a silent stop.
        """
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
        if not doc or doc.processing_status == STATUS_CANCELLING:
            return True
        return expected_revision is not None and doc.revision != expected_revision

    async def _mark_doc_failed(self, project_id: str, doc_id: str, log_msg: str,
                               expected_revision: int | None = None):
        """Mark a document as failed in the database.

        Guarded by the revision the pipeline started from: an edited document
        was reset and re-enqueued by the edit path, whose task owns the
        outcome, so this stale write is skipped.
        """
        try:
            async with UnitOfWork(project_id) as uow:
                applied = await uow.library.mark_failed(
                    doc_id, log_msg, expected_revision=expected_revision,
                )
            if not applied:
                logger.debug(
                    "Document %s changed or disappeared during indexing; "
                    "skipped the failure write", doc_id,
                )
        except Exception as e:
            logger.error("Failed to mark doc %s as failed: %s", doc_id, e, exc_info=True)

    # ------------------------------------------------------------------
    # Process a single document
    # ------------------------------------------------------------------

    async def process_one(self, project_id: str, doc_id: str,
                          expected_revision: int | None = None,
                          task_context=None) -> bool:
        """Index a single document for RAG.

        Returns True on success or permanent failure (caller should not retry).
        Returns False on transient failure (periodic scanner will retry).
        """
        index_written = False
        try:
            if task_context and await task_context.is_cancelling():
                return True

            # 1. Load the document
            async with UnitOfWork(project_id) as uow:
                doc = await uow.library.get_by_id(doc_id)

            if not doc:
                logger.debug("Document %s not found, skipping", doc_id)
                return True
            if expected_revision is None:
                expected_revision = doc.revision
            expected_generation = doc.index_generation
            previous_index = (doc.indexed_revision, doc.indexed_generation)
            if expected_revision is not None and doc.revision != expected_revision:
                logger.info("Document %s revision changed, skipping stale index task", doc_id)
                return True

            # 2. If no content, nothing to index — mark completed. The
            # blankness rule is the one shared with the sweep's SQL filter
            # (CONTENT_BLANK_CHARS): a divergent judgment would let the
            # sweep re-enqueue documents this step completes with zero
            # chunks, forever.
            if is_blank_content(doc.content):
                logger.info("Document %s has no content, marking completed", doc_id)
                async with UnitOfWork(project_id) as uow:
                    applied = await uow.library.update_processing_status(
                        doc_id, STATUS_COMPLETED, completed_at=utcnow(),
                        expected_revision=expected_revision,
                    )
                if not applied:
                    logger.debug(
                        "Document %s changed during indexing; its new task "
                        "owns the completion", doc_id,
                    )
                return True

            # 3. Create progress callback for task heartbeat between embedding batches
            loop = asyncio.get_running_loop()

            def progress_cb():
                if task_context is None:
                    return
                future = asyncio.run_coroutine_threadsafe(
                    task_context.heartbeat(), loop
                )
                try:
                    alive = future.result(timeout=5.0)
                except Exception as e:
                    logger.warning("Task heartbeat failed for %s: %s", doc_id, e, exc_info=True)
                    return
                if not alive:
                    loop.call_soon_threadsafe(task_context.cancel_event.set)

            def should_continue() -> bool:
                # A lost lease stops indexing exactly like a cancelled
                # document: the new claimant owns the durable row and the
                # document outcome, so this run must not write more chunks.
                if task_context is not None and task_context.cancel_event.is_set():
                    return False
                # Re-raises on failure: a status-check error must fail the
                # indexing task instead of looking like a clean stop.
                future = asyncio.run_coroutine_threadsafe(
                    self._doc_gone_or_stale(project_id, doc_id, expected_revision),
                    loop,
                )
                return not future.result(timeout=5.0)

            # 4. Index in RAG
            from app.services.rag_service import rag_service
            index_written = bool(await rag_service.index_document(
                project_id, doc_id, doc.content,
                title=doc.title, description=doc.description or "",
                progress_callback=progress_cb,
                should_continue=should_continue,
                doc_revision=expected_revision,
                index_generation=doc.index_generation,
            ))
            logger.info("RAG indexing completed for %s in project %s", doc_id, project_id)
            if not index_written:
                logger.info("RAG write did not produce a publishable generation for %s", doc_id)
                return True

            # 5. Post-indexing checks: the document may have been cancelled,
            #    deleted, or superseded while embeddings were computed.
            if task_context and await task_context.is_cancelling():
                if task_context.cancel_event.is_set():
                    # Lease lost to a new claimant mid-indexing: it owns the
                    # row and the document outcome, so this stale run must not
                    # touch chunks or document status — the claimant's own
                    # purge-before-add supersedes anything this run computed.
                    logger.info(
                        "Document %s index task lost its lease; stopping without finalizing",
                        doc_id,
                    )
                    return True
                logger.info("Document %s task cancelled during indexing, removing chunks", doc_id)
                if index_written:
                    await rag_service.cleanup_generation(
                        project_id, doc_id, expected_revision, expected_generation,
                    )
                return True

            async with UnitOfWork(project_id) as uow:
                doc_check = await uow.library.get_by_id(doc_id)
            if not doc_check:
                logger.info("Document %s deleted during indexing, removing chunks", doc_id)
                if index_written:
                    await rag_service.cleanup_generation(
                        project_id, doc_id, expected_revision, expected_generation,
                    )
                return True
            if doc_check.processing_status == STATUS_CANCELLING:
                logger.info("Document %s is cancelling, removing chunks", doc_id)
                if index_written:
                    await rag_service.cleanup_generation(
                        project_id, doc_id, expected_revision, expected_generation,
                    )
                return True
            if expected_revision is not None and doc_check.revision != expected_revision:
                logger.info("Document %s changed during indexing, skipping completion", doc_id)
                if index_written:
                    await rag_service.cleanup_generation(
                        project_id, doc_id, expected_revision, expected_generation,
                    )
                return True

            # 6. Mark completed
            async with UnitOfWork(project_id) as uow:
                published = await uow.library.publish_index(
                    doc_id, expected_revision, expected_generation,
                )
            if not published:
                logger.info("Document %s index publication lost its CAS race", doc_id)
                await rag_service.cleanup_generation(
                    project_id, doc_id, expected_revision, expected_generation,
                )
                return True

            old_revision, old_generation = previous_index
            if (
                old_revision is not None
                and old_generation is not None
                and (old_revision, old_generation) != (expected_revision, expected_generation)
            ):
                try:
                    await rag_service.cleanup_generation(
                        project_id, doc_id, old_revision, old_generation,
                    )
                except Exception:
                    logger.warning(
                        "Published index cleanup deferred for %s revision=%s generation=%s",
                        doc_id, old_revision, old_generation, exc_info=True,
                    )

            return True

        except Exception as e:
            error_msg = str(e)
            logger.error("Failed to index %s in project %s: %s", doc_id, project_id, e, exc_info=True)
            try:
                if index_written and expected_revision is not None and "expected_generation" in locals():
                    from app.services.rag_service import rag_service
                    await rag_service.cleanup_generation(
                        project_id, doc_id, expected_revision, expected_generation,
                    )
            except Exception:
                logger.warning("Failed to clean unpublished index generation for %s", doc_id, exc_info=True)

            # If doc was deleted/cancelled, don't retry
            if await self._doc_gone_or_stale(project_id, doc_id, expected_revision):
                return True

            # Permanent error — no retry
            error_lower = error_msg.lower()
            if any(marker in error_lower for marker in _PERMANENT_ERROR_MARKERS):
                await self._mark_doc_failed(
                    project_id, doc_id,
                    f"Embedding dimension mismatch. Please rebuild index. Error: {e}",
                    expected_revision=expected_revision,
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
