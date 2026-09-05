"""Durable background task scheduling for library workflows."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.project_registry import get_project_status, is_project_active, iter_project_ids
from app.core.task_status import TERMINAL_STATUSES
from app.database.unit_of_work import UnitOfWork
from app.services.library_task_protocol import (
    KIND_DOCUMENT_PROCESS,
    KIND_RAG_INDEX,
    QUEUE_LIBRARY,
    RunningTaskContext,
    get_task_handler,
)


logger = get_logger(__name__)

_WAKE_IDLE_TIMEOUT_SECONDS = 300.0        # runner re-checks queues when idle
_MAINTENANCE_INTERVAL_SECONDS = 60.0      # periodic library-queue recovery
_DAILY_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # terminal-row pruning cadence
_DOCUMENT_DRAIN_TIMEOUT_SECONDS = 10.0
_DOCUMENT_DRAIN_POLL_SECONDS = 0.05
_PROJECT_CURSOR = 0


def _project_allows_persistence(project_id: str) -> bool:
    """Return true only while the project is registered and active."""
    status = get_project_status(project_id)
    if status is None:
        return not (settings.USERDATA_DIR / project_id).exists()
    return status == "active" and is_project_active(project_id)


class BackgroundTaskService:
    """Application-facing API for durable background tasks."""

    def __init__(self) -> None:
        self._wake_event: asyncio.Event | None = None
        self._wake_event_loop: asyncio.AbstractEventLoop | None = None
        self._maintenance_task: asyncio.Task[None] | None = None

    def _wake_event_for_running_loop(self) -> asyncio.Event:
        """Return the wake event for the running loop.

        asyncio.Event binds to the first event loop that waits on it, and
        this singleton outlives individual loops (pytest-asyncio runs each
        test on a fresh loop), so a stale loop-bound event would raise
        "bound to a different event loop" in the waiter on the next loop.
        Recreate the event when the running loop changed. A wake lost across
        such a boundary is only a lost optimization, never lost work: the
        runner's idle timeout re-checks the durable queue.
        """
        loop = asyncio.get_running_loop()
        if (
            self._wake_event is not None
            and self._wake_event_loop is not None
            and self._wake_event_loop is not loop
        ):
            self._wake_event = asyncio.Event()
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        self._wake_event_loop = loop
        return self._wake_event

    @property
    def wake_event(self) -> asyncio.Event:
        """Event the library runner waits on between claim passes."""
        return self._wake_event_for_running_loop()

    def wake(self) -> None:
        """End the library runner's idle wait.

        The durable DB row is the source of truth; the event only avoids
        idle waiting. Setting an asyncio.Event coalesces any number of
        concurrent wakes into a single runner pass. On a running loop the
        event is rebound via ``_wake_event_for_running_loop`` before the
        set, so a wake issued on the loop the runner will wait on is never
        discarded by that rebinding; without any loop (runner not yet
        started) the event is created lazily so the wake is not lost either.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._wake_event is None:
                self._wake_event = asyncio.Event()
        else:
            self._wake_event_for_running_loop()
        self._wake_event.set()

    async def enqueue_document_process(
        self,
        project_id: str,
        doc_id: str,
        *,
        priority: int = 100,
        wake: bool = True,
        explicit: bool = False,
    ) -> str | None:
        """Queue processing unless the document needs explicit recovery."""
        if not await self._document_allows_enqueue(project_id, doc_id, explicit=explicit):
            return None
        revision = await self._get_document_revision(project_id, doc_id)
        return await self.enqueue(
            project_id=project_id,
            kind=KIND_DOCUMENT_PROCESS,
            payload={"doc_id": doc_id, "doc_revision": revision},
            dedupe_key=f"{KIND_DOCUMENT_PROCESS}:{project_id}:{doc_id}:{revision}",
            priority=priority,
            max_attempts=3,
            wake=wake,
        )

    async def enqueue_rag_index(
        self,
        project_id: str,
        doc_id: str,
        *,
        priority: int = 100,
        wake: bool = True,
        explicit: bool = False,
    ) -> str | None:
        """Queue indexing unless the document needs explicit recovery."""
        if not await self._document_allows_enqueue(project_id, doc_id, explicit=explicit):
            return None
        revision = await self._get_document_revision(project_id, doc_id)
        return await self.enqueue(
            project_id=project_id,
            kind=KIND_RAG_INDEX,
            payload={"doc_id": doc_id, "doc_revision": revision},
            dedupe_key=f"{KIND_RAG_INDEX}:{project_id}:{doc_id}:{revision}",
            priority=priority,
            max_attempts=5,
            wake=wake,
        )

    async def _get_document_revision(self, project_id: str, doc_id: str) -> int:
        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
            return doc.revision if doc else 0

    async def _document_allows_enqueue(
        self, project_id: str, doc_id: str, *, explicit: bool,
    ) -> bool:
        if explicit:
            return True
        from app.core.document_status import ACTIVE_STATUSES

        async with UnitOfWork(project_id) as uow:
            doc = await uow.library.get_by_id(doc_id)
        return bool(doc and doc.processing_status in ACTIVE_STATUSES)

    async def enqueue(
        self,
        *,
        project_id: str,
        kind: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
        priority: int = 100,
        max_attempts: int = 3,
        wake: bool = True,
    ) -> str:
        async with UnitOfWork(project_id) as uow:
            task = await uow.background_tasks.enqueue(
                kind=kind,
                queue=QUEUE_LIBRARY,
                payload_json=json.dumps(payload),
                priority=priority,
                max_attempts=max_attempts,
                dedupe_key=dedupe_key,
            )
        if wake:
            self.wake()
        return task.id

    async def cancel_document_tasks(self, project_id: str, doc_id: str) -> int:
        prefixes = (
            f"{KIND_DOCUMENT_PROCESS}:{project_id}:{doc_id}",
            f"{KIND_RAG_INDEX}:{project_id}:{doc_id}",
        )
        cancelled = 0
        async with UnitOfWork(project_id, allow_inactive=True) as uow:
            for prefix in prefixes:
                cancelled += await uow.background_tasks.cancel_by_dedupe_prefix(prefix)
        return cancelled

    async def wait_for_document(self, project_id: str, doc_id: str,
                                timeout: float = _DOCUMENT_DRAIN_TIMEOUT_SECONDS) -> bool:
        """Wait until no handler can write for a document."""
        prefixes = (
            f"{KIND_DOCUMENT_PROCESS}:{project_id}:{doc_id}",
            f"{KIND_RAG_INDEX}:{project_id}:{doc_id}",
        )
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            async with UnitOfWork(project_id, allow_inactive=True) as uow:
                if not await uow.background_tasks.has_inflight_for_dedupe_prefixes(prefixes):
                    return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(_DOCUMENT_DRAIN_POLL_SECONDS)

    async def cancel_project_tasks(self, project_id: str) -> int:
        async with UnitOfWork(project_id, allow_inactive=True) as uow:
            return await uow.background_tasks.cancel_all_active()

    async def cleanup_project(self, project_id: str) -> int:
        async with UnitOfWork(project_id, allow_inactive=True) as uow:
            return await uow.background_tasks.cleanup_terminal(
                settings.BACKGROUND_TASK_CLEANUP_HOURS
            )

    # ------------------------------------------------------------------
    # Periodic maintenance
    # ------------------------------------------------------------------

    def start_maintenance(self) -> None:
        """Start the periodic library maintenance loop.

        Idempotent: a second call while the loop runs is a no-op.
        """
        if self._maintenance_task is not None and not self._maintenance_task.done():
            return
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop_maintenance(self) -> None:
        """Cancel the maintenance loop and wait for the current sweep to end."""
        task = self._maintenance_task
        if task is None:
            return
        self._maintenance_task = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _maintenance_loop(self) -> None:
        """Recover interrupted library work; run daily upkeep.

        Each sweep re-enqueues documents whose processing was interrupted by
        a crash or restart and reconciles cancelling documents, then wakes
        the runner so recovered tasks start without waiting for an external
        event. Terminal background-task rows, RAG orphan chunks, and orphan
        library files are cleaned once per day, with the last-run time
        tracked inside the loop.
        """
        last_cleanup = float("-inf")
        while True:
            try:
                await self._maintenance_sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Library maintenance sweep failed", exc_info=True)

            now = asyncio.get_running_loop().time()
            if now - last_cleanup >= _DAILY_CLEANUP_INTERVAL_SECONDS:
                last_cleanup = now
                try:
                    await asyncio.wait_for(
                        self._cleanup_all_projects(),
                        timeout=settings.LIBRARY_SCAN_TOTAL_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Daily background task cleanup timed out after %d s; "
                        "the rest of this round is skipped and the next round "
                        "continues the cleanup",
                        settings.LIBRARY_SCAN_TOTAL_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Background task cleanup failed", exc_info=True)

            await asyncio.sleep(_MAINTENANCE_INTERVAL_SECONDS)

    async def _maintenance_sweep(self) -> None:
        """Run one library-queue recovery pass over every active project."""
        from app.database.manager import get_db_manager

        db_mgr = await get_db_manager()
        cleaned = await db_mgr.cleanup_inactive_projects()
        if cleaned:
            logger.info("Cleaned DB state for inactive project(s): %s", cleaned)

        for project_id in iter_project_ids():
            try:
                await asyncio.wait_for(
                    self._scan_library_project(project_id),
                    timeout=settings.LIBRARY_SCAN_PROJECT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("Background queue scan timed out for project %s", project_id)
            except Exception as exc:
                logger.warning(
                    "Background queue scan failed for project %s: %s",
                    project_id, exc, exc_info=True,
                )

        self.wake()

    async def _scan_library_project(self, project_id: str) -> int:
        """Re-enqueue interrupted work and reconcile cancelling documents.

        Returns the number of recovered items. Re-running is safe: enqueue
        dedupes on the document revision, and cancelling documents past
        their lease are marked failed so they stay visible and recoverable —
        the sweep never deletes user documents.
        """
        from app.core.document_status import (
            STATUS_CANCELLING, STATUS_INDEXING,
            STATUS_PENDING, STATUS_PROCESSING,
        )
        from app.core.utils import utcnow

        if not is_project_active(project_id):
            return 0

        now = utcnow()
        async with UnitOfWork(project_id) as uow:
            # A CANCELLING row past its lease has no runner left to confirm
            # the cancel (heartbeat renews CANCELLING rows too); left alone
            # it would never be claimed, reset by enqueue, or pruned.
            recovered = await uow.background_tasks.cancel_stale_cancelling()
            if recovered:
                logger.warning(
                    "Sweep: %d background task(s) in project %s stuck in "
                    "cancelling past their lease were cancelled (their "
                    "runner is gone)",
                    recovered, project_id,
                )
            # Id/status projection only: this sweep runs every 60 seconds,
            # so it must not load full document rows (content included).
            sweep_docs = await uow.library.list_status_projection(
                (STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING,
                 STATUS_CANCELLING),
            )

        if not is_project_active(project_id):
            return 0

        cancelling_docs = []
        for doc in sweep_docs:
            if doc.processing_status == STATUS_INDEXING:
                if await self._settle_terminal_document_task(
                    project_id, doc, KIND_RAG_INDEX,
                ):
                    recovered += 1
                    continue
                await self.enqueue_rag_index(project_id, doc.id, wake=False)
                recovered += 1
            elif doc.processing_status == STATUS_CANCELLING:
                cancelling_docs.append(doc)
            else:
                if await self._settle_terminal_document_task(
                    project_id, doc, KIND_DOCUMENT_PROCESS,
                ):
                    recovered += 1
                    continue
                await self.enqueue_document_process(project_id, doc.id, wake=False)
                recovered += 1
        for doc in cancelling_docs:
            # Re-read before cancelling: the snapshot may be stale and the
            # document may already have been re-enqueued into an active
            # state, in which case cancelling its tasks would kill fresh work.
            async with UnitOfWork(project_id) as uow:
                fresh = await uow.library.get_by_id(doc.id)
            if not fresh or fresh.processing_status != STATUS_CANCELLING:
                continue
            await self.cancel_document_tasks(project_id, doc.id)
            started_at = fresh.processing_started_at
            if (
                started_at
                and (now - started_at).total_seconds() <= settings.BACKGROUND_TASK_LEASE_SECONDS
            ):
                continue
            async with UnitOfWork(project_id) as uow:
                await uow.library.mark_failed(
                    doc.id,
                    "Processing was cancelled but did not stop in time; "
                    "marked as failed. Reprocess to recover.",
                    expected_revision=fresh.revision,
                )
                recovered += 1
                logger.warning(
                    "Sweep: cancelling document %s in project %s past its lease was "
                    "reset to failed (documents are never deleted by the sweep)",
                    doc.id, project_id,
                )

        await self._reindex_emptied_collection(project_id)

        return recovered

    async def _settle_terminal_document_task(self, project_id: str, doc, kind: str) -> bool:
        """Keep a terminal queue row from silently starting a fresh attempt budget."""
        dedupe_key = f"{kind}:{project_id}:{doc.id}:{doc.revision}"
        async with UnitOfWork(project_id) as uow:
            task = await uow.background_tasks.get_by_dedupe_key(dedupe_key)
            if task is None or task.status not in TERMINAL_STATUSES:
                return False
            await uow.library.mark_failed(
                doc.id,
                task.error or "Background processing stopped. Reprocess to continue.",
                expected_revision=doc.revision,
            )
        return True

    async def _reindex_emptied_collection(self, project_id: str) -> int:
        """Report an empty RAG collection without spending embedding tokens.

        A deleted or reset-out-from-under RAG store leaves completed content
        documents returning no search results. Rebuilding is an explicit user
        action because automatically re-embedding completed or failed
        documents consumes provider tokens.
        """
        from app.core.document_status import STATUS_COMPLETED
        from app.services.rag_service import rag_service

        try:
            chunk_count = await rag_service.collection_count(project_id)
        except Exception as exc:
            logger.warning(
                "Sweep: could not read the RAG chunk count for project %s: %s",
                project_id, exc, exc_info=True,
            )
            return 0
        if chunk_count != 0:
            return 0

        async with UnitOfWork(project_id) as uow:
            indexable_ids = await uow.library.list_ids_with_content(STATUS_COMPLETED)
        if indexable_ids:
            logger.warning(
                "Sweep: RAG collection for project %s is empty with %d completed "
                "content document(s); explicit rebuild is required",
                project_id, len(indexable_ids),
            )
        return 0

    async def _cleanup_all_projects(self) -> None:
        """Daily upkeep: prune terminal task rows, RAG orphan chunks, and
        orphan library files."""
        from app.database.manager import get_db_manager

        db_mgr = await get_db_manager()
        cleaned = await db_mgr.cleanup_inactive_projects()
        if cleaned:
            logger.info("Cleaned DB state for inactive project(s): %s", cleaned)

        removed = 0
        for project_id in iter_project_ids():
            try:
                removed += await self.cleanup_project(project_id)
            except Exception as exc:
                logger.warning(
                    "Failed to cleanup background tasks for %s: %s",
                    project_id, exc, exc_info=True,
                )
            try:
                await self._cleanup_project_orphan_chunks(project_id)
            except Exception as exc:
                logger.warning(
                    "Failed to cleanup RAG orphan chunks for %s: %s",
                    project_id, exc, exc_info=True,
                )
            try:
                await self._cleanup_project_orphan_files(project_id)
            except Exception as exc:
                logger.warning(
                    "Failed to cleanup library orphan files for %s: %s",
                    project_id, exc, exc_info=True,
                )
        if removed:
            logger.info("Background tasks: cleaned %d terminal task(s)", removed)

    async def _cleanup_project_orphan_chunks(self, project_id: str) -> None:
        """Best-effort removal of RAG chunks whose document no longer exists.

        ``rag_service.cleanup_orphans`` runs in tight phases — chunk scan,
        fresh DB read, delete — so the valid-document set is never a stale
        snapshot. It returns the doc_ids whose chunks it removed; any of
        those that exists in the database now lost chunks to a race with a
        concurrent index, so it is re-enqueued through the normal queue
        path (revision dedupe makes the re-enqueue safe).
        """
        from app.services.rag_service import rag_service

        async def valid_doc_ids() -> set:
            async with UnitOfWork(project_id) as uow:
                ids = await uow.library.list_ids()
            return set(ids)

        removed_doc_ids = await rag_service.cleanup_orphans(project_id, valid_doc_ids)
        async with UnitOfWork(project_id) as uow:
            docs = await uow.library.get_all()
        from app.core.document_status import ACTIVE_STATUSES, STATUS_INDEXING
        retained = {}
        for doc in docs:
            if doc.is_folder:
                continue
            identities = set()
            if doc.indexed_revision is not None and doc.indexed_generation is not None:
                identities.add((doc.indexed_revision, doc.indexed_generation))
            if doc.processing_status in ACTIVE_STATUSES:
                identities.add((doc.revision, doc.index_generation))
            retained[doc.id] = identities
        removed_doc_ids |= await rag_service.cleanup_stale_generations(project_id, retained)
        if not removed_doc_ids:
            return

        surviving_docs = [
            doc for doc in docs
            if doc.id in removed_doc_ids and doc.processing_status in ACTIVE_STATUSES
        ]
        reindexed = 0
        for doc in surviving_docs:
            if not (doc.content or "").strip():
                continue
            if doc.processing_status != STATUS_INDEXING:
                continue
            await self.enqueue_rag_index(project_id, doc.id, wake=False)
            reindexed += 1
            logger.warning(
                "Orphan cleanup removed chunks for document %s in project %s, "
                "but the document still exists; re-enqueued it for indexing",
                doc.id, project_id,
            )
        if reindexed:
            self.wake()

    async def _cleanup_project_orphan_files(self, project_id: str) -> None:
        """Best-effort removal of library files no document row points at.

        A crash between writing an uploaded file and committing its DB row
        leaves such a file; without the daily pass nothing reclaims it.
        """
        from app.services.library_service import library_service

        await library_service.cleanup_orphan_files(project_id)


class LibraryTaskRunner:
    """Library task runner loop.

    A long-lived asyncio task claims durable DB tasks until no more
    capacity or queued work remains, then waits on the wake event (or the
    idle timeout) before claiming again. It is started once at application
    startup and stopped at shutdown.
    """

    def __init__(self):
        self.owner = f"{socket.gethostname()}:{id(self)}"
        self._runner_task: asyncio.Task[None] | None = None
        self._active_by_project: dict[str, set[asyncio.Task[None]]] = {}
        self._cancel_events_by_project: dict[str, set[asyncio.Event]] = {}

    def start(self) -> None:
        """Start the runner loop as a task on the running event loop.

        Idempotent: a second call while the loop runs is a no-op.
        """
        if self._runner_task is not None and not self._runner_task.done():
            return
        background_task_service.wake()
        self._runner_task = asyncio.create_task(self._safe_run())

    async def stop(self) -> None:
        """Cancel the runner loop, cancelling in-flight tasks with it.

        Cancelling ``run`` marks each in-flight task failed-or-retry, so no
        claimed task is left stuck in running state past its lease.
        """
        task = self._runner_task
        if task is None:
            return
        self._runner_task = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def wait_for_project(self, project_id: str, timeout: float = 10.0) -> bool:
        """Cancel and drain handlers belonging to one project."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            active = list(self._active_by_project.get(project_id, ()))
            active = [task for task in active if not task.done()]
            if not active:
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                for task in active:
                    task.cancel()
                # Keep the task registered until its coroutine actually
                # exits; project deletion must not outrun a late write.
                return False
            await asyncio.wait(active, timeout=remaining)

    def cancel_project(self, project_id: str) -> None:
        """Publish the project barrier to every live library handler."""
        for event in tuple(self._cancel_events_by_project.get(project_id, ())):
            event.set()

    async def _safe_run(self) -> None:
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Library background runner stopped with error: %s", exc)

    async def run(self) -> None:
        active: set[asyncio.Task[None]] = set()
        try:
            limit = settings.LIBRARY_CONCURRENCY
            batch_size = settings.LIBRARY_QUEUE_BATCH_SIZE
            while True:
                wake_event = background_task_service.wake_event
                try:
                    await asyncio.wait_for(
                        wake_event.wait(),
                        timeout=_WAKE_IDLE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass  # idle tick: re-check queues for lease-expired work
                wake_event.clear()

                processed = 0
                while True:
                    while len(active) < limit and processed + len(active) < batch_size:
                        claimed = await self._claim_next()
                        if claimed is None:
                            break
                        project_id, task = claimed
                        child = asyncio.create_task(self._run_one(project_id, task))
                        self._active_by_project.setdefault(project_id, set()).add(child)
                        child.add_done_callback(
                            lambda finished, pid=project_id: self._forget_active(pid, finished),
                        )
                        active.add(child)

                    if not active:
                        break

                    done, active = await asyncio.wait(
                        active,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    processed += len(done)
                    for finished in done:
                        # _run_one owns its failure handling; a raised
                        # exception means the runner shell itself broke, so
                        # log it and keep the loop alive rather than ending
                        # the runner. A cancelled child has already finalized
                        # its DB row in _run_one's CancelledError handler.
                        if finished.cancelled():
                            logger.warning(
                                "Library background task wrapper was cancelled"
                            )
                            continue
                        exc = finished.exception()
                        if exc is not None:
                            logger.warning(
                                "Library background task wrapper crashed: %s",
                                exc, exc_info=exc,
                            )
                    if processed >= batch_size:
                        # Batch budget exhausted with work likely remaining:
                        # loop around immediately instead of idling.
                        background_task_service.wake()
                        break
        except asyncio.CancelledError:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            raise

    def _forget_active(self, project_id: str, task: asyncio.Task[None]) -> None:
        tasks = self._active_by_project.get(project_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            self._active_by_project.pop(project_id, None)

    async def _claim_next(self):
        """Claim the next runnable task across all projects.

        Returns ``(project_id, task)`` or ``None``. The project_id is the
        directory slot the task was claimed from — the source of truth for
        which DB to write results back to, since the task row's own
        ``project_id`` column is retained only for NOT NULL and is not read.
        """
        global _PROJECT_CURSOR
        project_ids = list(iter_project_ids())
        if not project_ids:
            return None
        start = _PROJECT_CURSOR % len(project_ids)
        ordered_project_ids = project_ids[start:] + project_ids[:start]
        for offset, project_id in enumerate(ordered_project_ids):
            while True:
                try:
                    async with UnitOfWork(project_id) as uow:
                        task = await uow.background_tasks.claim_next(
                            queue=QUEUE_LIBRARY,
                            owner=self.owner,
                            lease_seconds=settings.BACKGROUND_TASK_LEASE_SECONDS,
                        )
                    if not task:
                        break
                    if task.status == "failed":
                        await self._apply_final_failure(
                            project_id,
                            task,
                            task.error or "Task failed after lease expiry",
                        )
                        continue
                    _PROJECT_CURSOR = (start + offset + 1) % len(project_ids)
                    return project_id, task
                except Exception as exc:
                    logger.warning(
                        "Failed to claim library background task for %s: %s",
                        project_id,
                        exc,
                        exc_info=True,
                    )
                    break
        return None

    async def _run_one(self, project_id: str, task) -> None:
        # task.lease_owner is the one-time claim token written by claim_next:
        # it is the only identity allowed to heartbeat and finalize this run,
        # so a lease re-claimed elsewhere (even by this same runner) cannot be
        # extended or terminalized by this coroutine.
        owner = task.lease_owner
        ctx = RunningTaskContext(
            project_id=project_id,
            task_id=task.id,
            owner=owner,
            lease_seconds=settings.BACKGROUND_TASK_LEASE_SECONDS,
        )
        self._cancel_events_by_project.setdefault(project_id, set()).add(
            ctx.cancel_event,
        )
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            payload = json.loads(task.payload_json or "{}")
            handler = get_task_handler(task.kind)
            if handler is None:
                raise RuntimeError(f"Unknown background task kind: {task.kind}")
            heartbeat_task = asyncio.create_task(self._heartbeat_while_running(ctx))
            await handler(ctx, payload)
            if not _project_allows_persistence(project_id):
                return
            # allow_inactive: these finalizes serve tasks of a project that
            # may already be marked deleting; they must land while the
            # project's DB file still exists.
            async with UnitOfWork(project_id, allow_inactive=True) as uow:
                if await uow.background_tasks.is_cancelling(task.id):
                    await uow.background_tasks.mark_cancelled(task.id, owner)
                elif not await uow.background_tasks.mark_completed(task.id, owner):
                    # A cancel flipped the row to CANCELLING between the check
                    # above and this write, or the lease was re-claimed; the
                    # cancel/reclaim path owns the outcome now.
                    logger.debug(
                        "Background task %s was no longer completable at "
                        "finalize; left to its cancel/reclaim path", task.id,
                    )
        except asyncio.CancelledError:
            if not _project_allows_persistence(project_id):
                raise
            async with UnitOfWork(project_id, allow_inactive=True) as uow:
                # A CANCELLING row means the user already asked to cancel;
                # however this runner exits, the outcome is that confirmed
                # cancellation. Only a row still RUNNING (a pure external
                # cancel/shutdown with no cancel request recorded) falls
                # through to the attempt-consuming retry path.
                if not await uow.background_tasks.mark_cancelled(task.id, owner):
                    await uow.background_tasks.mark_failed_or_retry(
                        task.id, owner, "Task cancelled"
                    )
            raise
        except Exception as exc:
            logger.exception("Background task %s failed", task.id)
            if not _project_allows_persistence(project_id):
                raise
            async with UnitOfWork(project_id, allow_inactive=True) as uow:
                # Same split as the cancellation above: a failing handler
                # must confirm a pending user cancel instead of leaving the
                # row stranded in CANCELLING, and a confirmed cancellation
                # writes no document failure — the sweep reconciles
                # cancelling documents on its own.
                if await uow.background_tasks.mark_cancelled(task.id, owner):
                    status = "cancelled"
                else:
                    status = await uow.background_tasks.mark_failed_or_retry(
                        task.id, owner, str(exc)
                    )
                    if status == "failed":
                        # Best-effort: the task row is already terminal. If this
                        # fails, the document stays INDEXING; the maintenance sweep
                        # re-enqueues it and the next run retries the finalize.
                        try:
                            await _mark_task_document_failed_if_current(uow, task, str(exc))
                        except Exception:
                            logger.warning(
                                "Background task %s failed but its document could "
                                "not be marked failed; the maintenance sweep will "
                                "re-enqueue it",
                                task.id, exc_info=True,
                            )
            if status == "queued":
                background_task_service.wake()
        finally:
            events = self._cancel_events_by_project.get(project_id)
            if events is not None:
                events.discard(ctx.cancel_event)
                if not events:
                    self._cancel_events_by_project.pop(project_id, None)
            if heartbeat_task:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _apply_final_failure(self, project_id: str, task, error: str) -> None:
        """Apply document-side effects for a task already marked failed."""
        try:
            async with UnitOfWork(project_id) as uow:
                await _mark_task_document_failed_if_current(uow, task, error)
        except Exception:
            # Best-effort: the task row is already terminal. If this fails,
            # the document stays INDEXING; the maintenance sweep re-enqueues
            # it and the next run retries the finalize.
            logger.warning(
                "Failed to mark the document of task %s as failed; the "
                "maintenance sweep will re-enqueue it",
                task.id, exc_info=True,
            )

    @staticmethod
    def _heartbeat_interval(lease_seconds: int) -> float:
        """Seconds between lease renewals for a running task."""
        return min(60, max(10, lease_seconds // 3))

    async def _heartbeat_while_running(self, ctx: RunningTaskContext) -> None:
        interval = self._heartbeat_interval(ctx.lease_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                alive = await ctx.heartbeat()
                if not alive:
                    logger.warning(
                        "Background task heartbeat lost ownership: task=%s project=%s",
                        ctx.task_id, ctx.project_id,
                    )
                    # The lease was re-claimed elsewhere and the durable row
                    # now belongs to the new claimant; signal the loss to the
                    # executing handler so it stops at its next cancellation
                    # checkpoint instead of racing it.
                    ctx.cancel_event.set()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Background task heartbeat failed: task=%s project=%s error=%s",
                    ctx.task_id, ctx.project_id, exc,
                    exc_info=True,
                )


async def _mark_task_document_failed_if_current(uow, task, error: str) -> None:
    try:
        payload = json.loads(task.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}

    if task.kind == KIND_DOCUMENT_PROCESS:
        message = f"Document processing failed after task retries: {error}"
    elif task.kind == KIND_RAG_INDEX:
        message = f"Indexing failed after task retries: {error}"
    else:
        return

    await _mark_document_failed_if_current(uow, payload, message)


async def _mark_document_failed_if_current(uow, payload: dict, message: str) -> None:
    doc_id = payload.get("doc_id")
    if not doc_id:
        return

    doc = await uow.library.get_by_id(doc_id)
    if not doc:
        return

    expected_revision = payload.get("doc_revision")
    if expected_revision is not None and doc.revision != expected_revision:
        return

    await uow.library.mark_failed(doc_id, message, expected_revision=expected_revision)


background_task_service = BackgroundTaskService()
library_task_runner = LibraryTaskRunner()
