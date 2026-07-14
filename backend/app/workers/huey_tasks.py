"""
Huey task definitions for SiGMA.

Tasks are decorated with @huey.task() or @huey.periodic_task() and are
executed by the Huey consumer process (huey_consumer.py).

Streaming tasks (LLM chat, agent calls) connect to the StreamServer
running in the web process to relay real-time tokens.

IMPORTANT: This module is imported by BOTH the web process (to enqueue
tasks) and the consumer process (to execute them).  Heavy service
imports are loaded at module level so the consumer process has them
warmed up before executing the first task.
"""

import asyncio
import concurrent.futures
import contextlib
import os
import sys
import json
import threading
from typing import AsyncIterator

# Ensure the vendored Huey is importable
_VENDORED = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "huey"))
if _VENDORED not in sys.path:
    sys.path.insert(0, _VENDORED)

from huey import crontab

# Import the shared Huey singleton
from app.workers.huey_config import huey

from app.core.document_status import (
    STATUS_PENDING, STATUS_PROCESSING, STATUS_INDEXING,
    STATUS_CANCELLING,
)
from app.core.task_status import STATUS_AWAITING_INPUT, TERMINAL_STATUSES

# Initialize logging for the worker process (web process does this in main.py)
from app.core.logging import setup_logging
setup_logging(process="worker")

# Pre-load service modules used by task workers (consumer process).
# In the web process these are already imported by other routes.
# Doing this at module level avoids slow first-import inside a thread,
# which would cause SSE timeouts waiting for the worker to connect.
_stream_client_module = __import__('app.workers.stream_client', fromlist=['StreamClient'])
StreamClient = _stream_client_module.StreamClient

# Library task executors register their durable-queue handlers at import time.
# The Huey consumer does not run FastAPI lifecycle imports, so load them here.
__import__('app.services.document_processing_service')
__import__('app.services.index_builder')


# ============================================================================
# Shared persistent event loop for worker async tasks
# ============================================================================
#
# Huey tasks execute on consumer-managed threads. Each ``asyncio.run()``
# call creates AND destroys a new event loop; process-global asyncio
# primitives (notably ``DatabaseManager._init_lock``) bind to the loop on
# first ``acquire`` and then raise ``RuntimeError: ... is bound to a
# different event loop`` on the next call that uses a different loop.
#
# Sharing one long-lived loop across every worker async path keeps those
# primitives valid for the worker process lifetime. The same pattern is
# already used by ``BrowserThread`` for Playwright state.
#
# LibraryTaskRunner (background_task_service) also schedules onto this
# loop via ``run_coroutine_threadsafe`` so its DB access no longer lives
# on a conflicting private loop.

_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_loop_lock = threading.Lock()


def get_worker_loop() -> asyncio.AbstractEventLoop:
    """Return the worker process's shared persistent event loop.

    Lazily starts a daemon thread running ``loop.run_forever``. The loop
    is never shut down — it dies with the worker process. Safe to call
    from any thread; the loop itself runs on its own dedicated thread.
    """
    global _worker_loop
    if _worker_loop is not None:
        return _worker_loop
    with _worker_loop_lock:
        if _worker_loop is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="huey-async-loop",
                daemon=True,
            )
            thread.start()
            _worker_loop = loop
    return _worker_loop


def run_on_worker_loop(coro, *, timeout: float | None = None):
    """Submit *coro* to the shared worker loop and block until it completes.

    Re-raises any exception raised by the coroutine. Used by Huey task
    functions that need to run async code and return a synchronous result
    to the Huey consumer thread.
    """
    future = asyncio.run_coroutine_threadsafe(coro, get_worker_loop())
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(f"Worker coroutine exceeded {timeout}s timeout")


def _parse_sse_event(sse_event: str) -> tuple[str, dict]:
    """Return (event_type, data) for one SSE frame."""
    event_type = ""
    data_lines: list[str] = []
    for line in (sse_event or "").splitlines():
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    if not data_lines:
        return event_type, {}
    try:
        data = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        data = {}
    return event_type, data if isinstance(data, dict) else {}


def _project_is_active(project_id: str) -> bool:
    from app.core.project_registry import is_project_active
    return is_project_active(project_id)


async def _mark_failed_if_active(project_id: str, task_id: str, error: str) -> None:
    if not _project_is_active(project_id):
        return
    from app.database.unit_of_work import UnitOfWork
    async with UnitOfWork(project_id) as uow:
        await uow.task_state.mark_failed(task_id, error)


def purge_project_tasks(project_id: str) -> int:
    """Remove queued and scheduled Huey tasks that still reference a project.

    Returns the number of tasks removed. Best-effort: per-row and per-table
    failures are logged and skipped so that a single corrupt row does not
    block project deletion.
    """
    logger = _get_logger()
    removed = 0
    for table in ("task", "schedule"):
        try:
            rows = huey.storage.sql(
                f"select id, data from {table} where queue = ?",
                (huey.storage.name,),
                results=True,
            )
            matching_ids = []
            for row_id, data in rows:
                try:
                    task = huey.deserialize_task(data)
                except Exception:
                    logger.debug(
                        "Failed to decode Huey %s row %s", table, row_id, exc_info=True,
                    )
                    continue
                if getattr(task, "kwargs", {}).get("project_id") == project_id:
                    matching_ids.append(row_id)
            if not matching_ids:
                continue
            with huey.storage.db(commit=True) as cursor:
                cursor.executemany(
                    f"delete from {table} where id = ?",
                    [(row_id,) for row_id in matching_ids],
                )
            removed += len(matching_ids)
        except Exception as exc:
            logger.warning(
                "Failed to purge Huey %s rows for project %s: %s",
                table, project_id, exc, exc_info=True,
            )
    return removed


# ============================================================================
# Streaming task runner — shared lifecycle for run_llm_chat & run_annotation_reply
# ============================================================================

class _StreamingTaskRunner:
    """Lifecycle owner for streaming Huey tasks.

    Owns the StreamClient connection, an independent heartbeat task that
    keeps the task alive during idle periods (LLM prefill, tool execution),
    project-active checks, and final state transitions (mark_completed /
    mark_failed). The caller supplies an async iterator of SSE-formatted
    strings plus optional callbacks for delta collection and error capture.
    """

    HEARTBEAT_INTERVAL = 5  # seconds

    def __init__(self, task_id: str, project_id: str, stream_port: int | None = None):
        self.task_id = task_id
        self.project_id = project_id
        self.client = StreamClient(port=stream_port) if stream_port else StreamClient()
        self.connected = False

    async def run(
        self,
        source: AsyncIterator[str],
        *,
        on_delta=None,
        on_error=None,
    ) -> dict:
        """Drain SSE source through StreamServer with full lifecycle.

        Args:
            source: async iterator yielding SSE-formatted strings.
            on_delta: optional callable(event_data: dict) -> str; return value
                is appended to the accumulated response on each "delta" event.
            on_error: optional callable(event_data: dict) -> dict. If provided,
                "error" events are captured (NOT forwarded as chunks) and the
                payload is sent via send_error after the source drains.

        Returns: Huey task result dict.
        """
        from app.database.unit_of_work import UnitOfWork

        if not _project_is_active(self.project_id):
            return {"status": "ignored", "reason": "project_inactive"}

        # Honor a cancel that landed while this task was queued or connecting,
        # and bail out if the task is already terminal (e.g. cancelled straight
        # to terminal from awaiting_input, or finalized by an earlier run). In
        # both cases the worker must not connect or run, so a late Huey job does
        # not produce an orphan run on a task the user already cancelled.
        # STATUS_CANCELLING is imported locally because document_status exports
        # a same-named constant for a different domain (library documents).
        from app.core.task_status import STATUS_CANCELLING as TASK_CANCELLING

        async with UnitOfWork(self.project_id) as uow:
            status = await uow.task_state.get_status(self.task_id)
            if status == TASK_CANCELLING:
                await uow.task_state.mark_cancelled(self.task_id)
                return {"status": "cancelled", "reason": "cancelled_before_start"}
            if status in TERMINAL_STATUSES:
                return {"status": "ignored", "reason": f"already_{status}"}

        self.connected = await self.client.connect(self.task_id, self.project_id)
        async with UnitOfWork(self.project_id) as uow:
            await uow.task_state.heartbeat(self.task_id)
            if await uow.task_state.is_cancelling(self.task_id):
                self.client.cancel_event.set()

        stop_heartbeat = asyncio.Event()
        hb_task = asyncio.create_task(self._heartbeat_loop(stop_heartbeat))

        full_response = ""
        error_payload: dict | None = None
        last_stream_hb = asyncio.get_running_loop().time()

        try:
            async for sse_event in source:
                if not _project_is_active(self.project_id):
                    self.client.cancel_event.set()
                    return {"status": "ignored", "reason": "project_inactive"}

                event_type, event_data = _parse_sse_event(sse_event)

                # Capture error events for end-of-stream fail signal.
                # Error events are NOT forwarded as chunks; the payload is
                # delivered via send_error after the source drains.
                if event_type == "error" and on_error is not None:
                    error_payload = on_error(event_data)
                    continue

                if self.connected:
                    await self.client.send_chunk(sse_event)

                if event_type == "delta" and on_delta is not None and isinstance(event_data, dict):
                    full_response += on_delta(event_data)

                last_stream_hb = await self._maybe_heartbeat(last_stream_hb)

            if error_payload is not None:
                return await self._fail(error_payload)
            return await self._complete(full_response)

        except Exception as exc:
            _get_logger().exception(
                "Streaming task %s failed for project %s",
                self.task_id, self.project_id,
            )
            await _mark_failed_if_active(self.project_id, self.task_id, str(exc))
            # send_error is best-effort: if _complete/_fail already closed the
            # client, self.connected is False and this is a no-op.
            if self.connected:
                await self.client.send_error(str(exc))
            return {"error": str(exc), "status": "failed"}
        finally:
            stop_heartbeat.set()
            hb_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb_task
            await self.client.close()

    async def _heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        """Independent heartbeat — covers idle periods when no chunks flow."""
        from app.database.unit_of_work import UnitOfWork

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.HEARTBEAT_INTERVAL)
                return  # stopped
            except asyncio.TimeoutError:
                pass
            try:
                if not _project_is_active(self.project_id):
                    self.client.cancel_event.set()
                    return
                async with UnitOfWork(self.project_id) as uow:
                    await uow.task_state.heartbeat(self.task_id)
                    # Bridge cancel intent recorded in the DB to the in-memory
                    # event the loop observes. Covers a TCP signal lost because
                    # the worker connection dropped mid-run.
                    if await uow.task_state.is_cancelling(self.task_id):
                        self.client.cancel_event.set()
                if self.connected:
                    await self.client.send_heartbeat()
            except Exception:
                _get_logger().debug(
                    "Heartbeat failed for task %s", self.task_id, exc_info=True,
                )

    async def _maybe_heartbeat(self, last_time: float) -> float:
        """Throttled inline heartbeat during active streaming."""
        from app.database.unit_of_work import UnitOfWork

        now = asyncio.get_running_loop().time()
        if now - last_time < self.HEARTBEAT_INTERVAL:
            return last_time
        try:
            async with UnitOfWork(self.project_id) as uow:
                await uow.task_state.heartbeat(self.task_id)
                # Bridge a DB-recorded cancel during active streaming, when
                # this inline heartbeat fires more often than the 5s loop and
                # the TCP signal may have been lost.
                if await uow.task_state.is_cancelling(self.task_id):
                    self.client.cancel_event.set()
            if self.connected:
                await self.client.send_heartbeat()
        except Exception:
            _get_logger().debug(
                "Stream heartbeat failed for task %s", self.task_id, exc_info=True,
            )
        return now

    async def _complete(self, full_response: str) -> dict:
        from app.database.unit_of_work import UnitOfWork

        if not _project_is_active(self.project_id):
            return {"status": "ignored", "reason": "project_inactive"}
        async with UnitOfWork(self.project_id) as uow:
            await uow.task_state.heartbeat(self.task_id)
            liveness = await uow.task_state.check_liveness(self.task_id)
            # Don't overwrite awaiting_input status (interactive tool paused)
            if liveness != STATUS_AWAITING_INPUT:
                await uow.task_state.mark_completed(self.task_id)
        await self.client.send_done()
        return {"response": full_response, "status": "completed"}

    async def _fail(self, error_payload: dict) -> dict:
        message = str(error_payload.get("error") or "Task failed")
        await _mark_failed_if_active(self.project_id, self.task_id, message)
        if self.connected:
            await self.client.send_error(message, data=error_payload)
        return {"error": message, "status": "failed"}


# ============================================================================
# LLM Chat — streaming sessions
# ============================================================================

@huey.task(retries=0)  # no auto-retry — user manually resumes on crash
def run_llm_chat(
    task_id: str,
    project_id: str,
    context: dict,
    stream_port: int | None = None,
    session_id: str = None,
    interaction_response: dict = None,
) -> dict:
    """
    Execute an AI chat conversation, streaming tokens back via StreamServer.

    *task_id*      — pre-generated UUID (shared with web process for state tracking)
    *project_id*   — project UUID
    *context*      — dict with keys: file, citation, cursor
    *stream_port*  — port of the StreamServer in the web process
    *session_id*   — session UUID (created if None)
    """
    async def _impl() -> dict:
        from app.services.chat_executor import stream_chat_for_task
        runner = _StreamingTaskRunner(task_id, project_id, stream_port)
        return await runner.run(
            stream_chat_for_task(
                project_id=project_id,
                context=context,
                session_id=session_id,
                interaction_response=interaction_response,
                task_id=task_id,
                cancel_event=runner.client.cancel_event,
            ),
            on_delta=lambda data: data.get("content", ""),
            on_error=lambda data: data or {"error": "LLM task failed"},
        )
    return run_on_worker_loop(_impl())


# ============================================================================
# Document Processing
# ============================================================================

# ── Annotation AI Reply ──

@huey.task(retries=0)
def run_annotation_reply(
    task_id: str,
    project_id: str,
    file_path: str,
    annotation_id: str,
    stream_port: int | None = None,
) -> dict:
    """
    Execute an annotation AI reply with streaming and tool support.

    Runs AnnotationLoop inside a Huey worker, streaming events via
    StreamServer — same transport as run_llm_chat.
    """
    async def _impl() -> dict:
        from app.services.annotation_loop import AnnotationLoop
        runner = _StreamingTaskRunner(task_id, project_id, stream_port)
        loop = AnnotationLoop(
            project_id=project_id,
            file_path=file_path,
            annotation_id=annotation_id,
            cancel_event=runner.client.cancel_event,
        )
        return await runner.run(
            _annotation_sse_source(loop),
            on_delta=lambda data: data.get("content", ""),
            on_error=lambda data: data or {"error": "Annotation task failed"},
        )
    return run_on_worker_loop(_impl())


async def _annotation_sse_source(loop) -> AsyncIterator[str]:
    """Convert AnnotationLoop dict events to SSE-formatted strings.

    AnnotationLoop yields {"type": str, "data": dict}; the wire format is
    the same SSE framing that ai_service.stream_chat emits, so the runner
    can treat both sources uniformly.
    """
    async for event in loop.run():
        event_type = event.get("type", "")
        event_data = event.get("data", {})
        yield f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"


# ============================================================================
# Durable Library Background Queue
# ============================================================================

@huey.task(retries=0)
def run_library_queue() -> dict:
    """Start library background work without occupying this Huey worker."""
    from app.services.background_task_service import library_task_runner

    return library_task_runner.start_in_thread()


@huey.periodic_task(crontab(minute="*"))  # every minute
def scan_and_queue_indexing():
    """Recover durable library queues and reconcile active document states."""
    from app.core.config import settings

    async def _scan():
        from app.services.background_task_service import background_task_service
        from app.database.manager import get_db_manager
        from app.database.unit_of_work import UnitOfWork

        db_mgr = await get_db_manager()
        cleaned = await db_mgr.cleanup_inactive_projects()
        if cleaned:
            _get_logger().info("Cleaned worker DB state for inactive project(s): %s", cleaned)

        recovered = 0
        reaped = 0
        for pid in _iter_library_scan_project_ids():
            try:
                # Reap chat tasks orphaned by a worker crash. Uses a separate
                # UnitOfWork so the reaper's commits are isolated from the
                # library scan. Safe to run unconditionally: reap_stale_tasks
                # only touches tasks check_liveness already deems dead.
                async with UnitOfWork(pid) as uow:
                    reaped += await uow.task_state.reap_stale_tasks()
            except Exception as exc:
                _get_logger().warning(
                    "Stale task reap failed for project %s: %s", pid, exc,
                )
            try:
                recovered += await asyncio.wait_for(
                    _scan_library_project(pid),
                    timeout=settings.LIBRARY_SCAN_PROJECT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                _get_logger().warning("Background queue scan timed out for project %s", pid)
            except Exception as exc:
                _get_logger().warning(
                    "Background queue scan failed for project %s: %s",
                    pid, exc, exc_info=True,
                )

        background_task_service.wake()
        if recovered:
            _get_logger().info("Periodic scanner reconciled %d library task(s)", recovered)
        if reaped:
            _get_logger().info("Periodic scanner reaped %d stale task(s)", reaped)

    try:
        run_on_worker_loop(
            _scan(),
            timeout=settings.LIBRARY_SCAN_TOTAL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        _get_logger().warning("Background queue scan exceeded total timeout: %s", exc)


def _iter_library_scan_project_ids() -> list[str]:
    from app.core.config import settings

    if not settings.USERDATA_DIR.exists():
        return []
    project_ids: list[str] = []
    for project_dir in sorted(settings.USERDATA_DIR.iterdir()):
        if not project_dir.is_dir() or project_dir.name == ".SiGMA":
            continue
        pid = project_dir.name
        if not _project_is_active(pid):
            continue
        project_db = project_dir / ".SiGMA" / "project_data.db"
        if project_db.exists():
            project_ids.append(pid)
    return project_ids


async def _scan_library_project(pid: str) -> int:
    from app.core.config import settings
    from app.core.utils import utcnow
    from app.database.unit_of_work import UnitOfWork
    from app.services.background_task_service import background_task_service
    from app.services.library_service import library_service

    if not _project_is_active(pid):
        return 0

    now = utcnow()
    async with UnitOfWork(pid) as uow:
        pending_docs = await uow.library.list_by_status(STATUS_PENDING)
        processing_docs = await uow.library.list_by_status(STATUS_PROCESSING)
        indexing_docs = await uow.library.list_by_status(STATUS_INDEXING)
        cancelling_docs = await uow.library.list_by_status(STATUS_CANCELLING)

    if not _project_is_active(pid):
        return 0

    recovered = 0
    for doc in pending_docs + processing_docs:
        await background_task_service.enqueue_document_process(pid, doc.id, wake=False)
        recovered += 1
    for doc in indexing_docs:
        await background_task_service.enqueue_rag_index(pid, doc.id, wake=False)
        recovered += 1
    for doc in cancelling_docs:
        await background_task_service.cancel_document_tasks(pid, doc.id)
        started_at = doc.processing_started_at
        if (
            started_at
            and (now - started_at).total_seconds() > settings.BACKGROUND_TASK_LEASE_SECONDS
        ):
            await library_service.delete_single(pid, doc.id)
            recovered += 1

    return recovered


@huey.periodic_task(crontab(minute="0", hour="3"))  # daily at 3 AM
def cleanup_old_results():
    """Clean up Huey task results to prevent DB bloat.

    SqliteHuey has no built-in TTL, so we flush all results periodically.
    Results are only needed for short-lived queries (e.g. checking task status
    from the web process). After 24h they are never read again.
    """
    from app.core.config import settings

    huey.storage.flush_results()
    logger = _get_logger()
    logger.info("Huey: flushed all task results")

    async def _cleanup_background_tasks():
        from app.database.manager import get_db_manager
        from app.services.background_task_service import background_task_service

        db_mgr = await get_db_manager()
        cleaned = await db_mgr.cleanup_inactive_projects()
        if cleaned:
            logger.info("Cleaned worker DB state for inactive project(s): %s", cleaned)

        removed = 0
        for project_dir in settings.USERDATA_DIR.iterdir():
            if not project_dir.is_dir() or project_dir.name == ".SiGMA":
                continue
            if not _project_is_active(project_dir.name):
                continue
            project_db = project_dir / ".SiGMA" / "project_data.db"
            if not project_db.exists():
                continue
            try:
                removed += await background_task_service.cleanup_project(project_dir.name)
            except Exception as exc:
                logger.warning(
                    "Failed to cleanup background tasks for %s: %s",
                    project_dir.name,
                    exc,
                    exc_info=True,
                )
        if removed:
            logger.info("Background tasks: cleaned %d terminal task(s)", removed)

    try:
        run_on_worker_loop(
            _cleanup_background_tasks(),
            timeout=settings.LIBRARY_SCAN_TOTAL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        logger.warning("Background task cleanup exceeded timeout: %s", exc)


# ============================================================================
# Logging helper (lazy)
# ============================================================================

def _get_logger():
    from app.core.logging import get_logger
    return get_logger("huey_tasks")
