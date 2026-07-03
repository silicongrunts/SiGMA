"""Durable background task scheduling for library workflows."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.database.unit_of_work import UnitOfWork
from app.services.library_task_protocol import (
    KIND_DOCUMENT_PROCESS,
    KIND_RAG_INDEX,
    QUEUE_LIBRARY,
    RunningTaskContext,
    get_task_handler,
)


logger = get_logger(__name__)

_RUN_LOCK = threading.Lock()
_WAKE_LOCK = threading.Lock()
_WAKE_TIMER: threading.Timer | None = None
_WAKE_DEBOUNCE_SECONDS = 0.25
_PROJECT_CURSOR = 0


class BackgroundTaskService:
    """Application-facing API for durable background tasks."""

    async def enqueue_document_process(
        self,
        project_id: str,
        doc_id: str,
        *,
        action: str = "process",
        priority: int = 100,
        wake: bool = True,
    ) -> str:
        revision = await self._get_document_revision(project_id, doc_id)
        return await self.enqueue(
            project_id=project_id,
            kind=KIND_DOCUMENT_PROCESS,
            payload={"doc_id": doc_id, "action": action, "doc_revision": revision},
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
    ) -> str:
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
                project_id=project_id,
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

    async def cancel_project_tasks(self, project_id: str) -> int:
        async with UnitOfWork(project_id, allow_inactive=True) as uow:
            return await uow.background_tasks.cancel_all_active()

    async def cleanup_project(self, project_id: str) -> int:
        async with UnitOfWork(project_id, allow_inactive=True) as uow:
            return await uow.background_tasks.cleanup_terminal(
                settings.BACKGROUND_TASK_CLEANUP_HOURS
            )

    def wake(self) -> None:
        """Best-effort Huey wake-up.

        The durable DB row is the source of truth.  Waking Huey must never
        block the API path; if it fails, the periodic scanner will wake it.
        """
        global _WAKE_TIMER
        with _WAKE_LOCK:
            if _WAKE_TIMER is not None:
                return
            _WAKE_TIMER = threading.Timer(_WAKE_DEBOUNCE_SECONDS, self._flush_wake)
            _WAKE_TIMER.daemon = True
            _WAKE_TIMER.start()

    def _flush_wake(self) -> None:
        global _WAKE_TIMER
        try:
            from app.workers.huey_tasks import run_library_queue

            if not _queued_library_wake_exists():
                run_library_queue()
        except Exception as exc:
            logger.warning("Failed to wake library background queue: %s", exc, exc_info=True)
        finally:
            with _WAKE_LOCK:
                _WAKE_TIMER = None


class LibraryTaskRunner:
    """Worker-side library task runner.

    One Huey task runs this loop. It claims durable DB tasks until no more
    capacity or queued work remains, then yields back to Huey.
    """

    def __init__(self):
        self.owner = f"{socket.gethostname()}:{id(self)}"

    def start_in_thread(self) -> dict[str, int | str]:
        """Start the library runner on the shared worker event loop.

        Returns immediately; the runner executes concurrently on the
        persistent worker loop (see ``huey_tasks.get_worker_loop``).
        Sharing that loop keeps ``DatabaseManager._init_lock`` bound to a
        single loop for the whole worker process — running on a separate
        private loop here would conflict with the periodic scanner and
        other Huey tasks that also touch the DB.
        """
        if _RUN_LOCK.locked():
            return {"queue": QUEUE_LIBRARY, "processed": 0, "status": "already_running"}

        from app.workers.huey_tasks import get_worker_loop

        async def _safe_run():
            try:
                await self.run()
            except Exception as exc:
                logger.exception("Library background runner stopped with error: %s", exc)

        # Fire-and-forget on the shared loop; the Huey task returns at once.
        asyncio.run_coroutine_threadsafe(_safe_run(), get_worker_loop())
        return {"queue": QUEUE_LIBRARY, "processed": 0, "status": "started"}

    async def run(self) -> dict[str, int | str]:
        if not _RUN_LOCK.acquire(blocking=False):
            return {"queue": QUEUE_LIBRARY, "processed": 0, "status": "already_running"}
        active: set[asyncio.Task[None]] = set()
        try:
            limit = settings.LIBRARY_WORKERS
            batch_size = settings.LIBRARY_QUEUE_BATCH_SIZE
            processed = 0
            while True:
                while len(active) < limit and processed + len(active) < batch_size:
                    task = await self._claim_next()
                    if task is None:
                        break
                    active.add(asyncio.create_task(self._run_one(task)))

                if not active:
                    break

                done, active = await asyncio.wait(
                    active,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                processed += len(done)
                for finished in done:
                    finished.result()
                if processed >= batch_size:
                    background_task_service.wake()
                    return {"queue": QUEUE_LIBRARY, "processed": processed, "status": "yielded"}
            return {"queue": QUEUE_LIBRARY, "processed": processed, "status": "done"}
        except asyncio.CancelledError:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            raise
        finally:
            _RUN_LOCK.release()

    async def _claim_next(self):
        global _PROJECT_CURSOR
        project_ids = _iter_project_ids()
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
                            task,
                            task.error or "Task failed after lease expiry",
                        )
                        continue
                    _PROJECT_CURSOR = (start + offset + 1) % len(project_ids)
                    return task
                except Exception as exc:
                    logger.warning(
                        "Failed to claim library background task for %s: %s",
                        project_id,
                        exc,
                        exc_info=True,
                    )
                    break
        return None

    async def _run_one(self, task) -> None:
        ctx = RunningTaskContext(
            project_id=task.project_id,
            task_id=task.id,
            owner=self.owner,
            lease_seconds=settings.BACKGROUND_TASK_LEASE_SECONDS,
        )
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            payload = json.loads(task.payload_json or "{}")
            handler = get_task_handler(task.kind)
            if handler is None:
                raise RuntimeError(f"Unknown background task kind: {task.kind}")
            heartbeat_task = asyncio.create_task(self._heartbeat_while_running(ctx))
            await handler(ctx, payload)
            async with UnitOfWork(task.project_id) as uow:
                if await uow.background_tasks.is_cancelling(task.id):
                    await uow.background_tasks.mark_cancelled(task.id, self.owner)
                else:
                    await uow.background_tasks.mark_completed(task.id, self.owner)
        except asyncio.CancelledError:
            async with UnitOfWork(task.project_id) as uow:
                await uow.background_tasks.mark_failed_or_retry(
                    task.id, self.owner, "Worker task cancelled"
                )
            raise
        except Exception as exc:
            logger.exception("Background task %s failed", task.id)
            async with UnitOfWork(task.project_id) as uow:
                status = await uow.background_tasks.mark_failed_or_retry(
                    task.id, self.owner, str(exc)
                )
                if status == "failed":
                    await _mark_task_document_failed_if_current(uow, task, str(exc))
            if status == "queued":
                background_task_service.wake()
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _apply_final_failure(self, task, error: str) -> None:
        """Apply document-side effects for a task already marked failed."""
        async with UnitOfWork(task.project_id) as uow:
            await _mark_task_document_failed_if_current(uow, task, error)

    async def _heartbeat_while_running(self, ctx: RunningTaskContext) -> None:
        interval = min(60, max(10, ctx.lease_seconds // 3))
        while True:
            await asyncio.sleep(interval)
            try:
                alive = await ctx.heartbeat()
                if not alive:
                    logger.warning(
                        "Background task heartbeat lost ownership: task=%s project=%s",
                        ctx.task_id, ctx.project_id,
                    )
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Background task heartbeat failed: task=%s project=%s error=%s",
                    ctx.task_id, ctx.project_id, exc,
                    exc_info=True,
                )


def _iter_project_ids() -> list[str]:
    if not settings.USERDATA_DIR.exists():
        return []
    from app.core.project_registry import is_project_active

    project_ids = []
    for project_dir in sorted(settings.USERDATA_DIR.iterdir()):
        if not project_dir.is_dir() or project_dir.name == ".SiGMA":
            continue
        if not is_project_active(project_dir.name):
            continue
        db_path = project_dir / ".SiGMA" / "project_data.db"
        if db_path.exists():
            project_ids.append(project_dir.name)
    return project_ids


def _queued_library_wake_exists() -> bool:
    """Return True if Huey already has a pending library-runner wake task."""
    try:
        from app.workers.huey_config import huey

        rows = huey.storage.sql(
            "select data from task where queue = ?",
            (huey.storage.name,),
            results=True,
        )
        for (data,) in rows:
            try:
                task = huey.deserialize_task(data)
            except Exception:
                continue
            if getattr(task, "name", "") == "run_library_queue":
                return True
    except Exception:
        logger.debug("Failed to inspect Huey wake queue", exc_info=True)
    return False


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

    await uow.library.mark_failed(
        doc_id,
        message,
    )


background_task_service = BackgroundTaskService()
library_task_runner = LibraryTaskRunner()
