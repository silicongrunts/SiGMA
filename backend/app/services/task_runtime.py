"""In-process streaming task runtime.

Runs one task's event source as an asyncio task inside the web process: the
``TaskRunner`` consumes an async iterator of ``{"type", "data"}`` events,
frames each one into the task's ``StreamSession``, and owns the matching
``task_state`` transitions so the durable status always matches what
subscribers saw.

Module functions are the surface for request handlers and lifecycle hooks:
``launch`` starts a task, ``cancel`` / ``cancel_project`` signal cooperative
cancellation, ``shutdown_all`` drains every runner at process shutdown, and
``startup_reconcile`` finalizes rows left active by a run that did not
survive a process restart (interaction checkpoints in awaiting, consuming,
or failed states remain recoverable). While the process is alive, the periodic stranded-row sweep
(``start_stranded_sweep``) finalizes runnable rows whose runner disappeared
without finalizing them.

The ``_tasks`` map is process-local bookkeeping used for shutdown
coordination and the stranded-row sweep; the durable truth is the
per-project ``task_state`` table.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Optional

from app.core.chat_events import make_event
from app.core.logging import get_logger
from app.core.project_registry import is_project_active, iter_project_ids
from app.core.task_status import (
    ACTIVE_STATUSES,
    SSE_CANCELLED,
    SSE_DONE,
    SSE_ERROR,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLING,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_QUEUED,
    TERMINAL_EVENT_TYPES,
    TERMINAL_STATUSES,
)
from app.core.utils import utcnow
from app.database.unit_of_work import UnitOfWork
from app.services.stream_hub import StreamSession, stream_hub

logger = get_logger(__name__)

SHUTDOWN_GRACE_SECONDS = 10.0
RESTART_ERROR_MESSAGE = "Task was interrupted by an application restart."
STRANDED_ERROR_MESSAGE = "Task was interrupted because its runner stopped."

# Finalize writes retry through transient SQLite write contention; the
# periodic stranded-row sweep behind them finalizes a row whose attempts
# were all exhausted.
FINALIZE_ATTEMPTS = 3
FINALIZE_RETRY_DELAY_SECONDS = 0.5

# Liveness sweep cadence and the window a runnable row gets to be claimed by
# its runner: a row just inserted (set_queued) has a fresh updated_at, so the
# grace window excludes it from the sweep until a launch has had its chance.
STRANDED_SWEEP_INTERVAL_SECONDS = 60.0
STRANDED_GRACE_SECONDS = 75.0

_CANCELLED_EVENT = make_event(SSE_CANCELLED, {"message": "Task cancelled by user"})

# task_id -> asyncio.Task running the TaskRunner. Entries remove themselves in
# the runner's finally block.
_tasks: dict[str, asyncio.Task] = {}
_cancel_watchdogs: dict[str, asyncio.Task] = {}

_sweep_task: Optional[asyncio.Task] = None


def _terminal_event(status: str) -> dict:
    """Synthesize the terminal event for a task already terminal in the DB."""
    if status == STATUS_FAILED:
        return make_event(SSE_ERROR, {"error": "Task failed"})
    if status == STATUS_CANCELLED:
        return _CANCELLED_EVENT
    return make_event(SSE_DONE, {})


class TaskRunner:
    """Run one task's event source, own its stream session and state.

    ``source_factory`` receives the session's cancel event and returns the
    async iterator of ``{"type", "data"}`` events to stream. ``run`` performs
    the pre-flight state guards, consumes the source, and finalizes the
    ``task_state`` row before detaching the session from the hub.
    """

    def __init__(
        self,
        *,
        task_id: str,
        project_id: str,
        source_factory: Callable[[asyncio.Event], AsyncIterator[dict]],
        session: StreamSession,
    ):
        self.task_id = task_id
        self.project_id = project_id
        self._source_factory = source_factory
        self.session = session

    async def run(self) -> None:
        """Drive one task end-to-end: guards, streaming, finalization."""
        terminal_seen: Optional[str] = None
        error_message: Optional[str] = None
        try:
            if not is_project_active(self.project_id):
                await self._fail_before_start("Project is inactive.")
                return

            status = await self._read_status()
            if status == STATUS_CANCELLING:
                # Cancel won the race before the run started.
                await self._safe_mark(lambda repo: repo.mark_cancelled(self.task_id))
                self.session.push(_CANCELLED_EVENT)
                return
            if status in TERMINAL_STATUSES:
                self.session.push(_terminal_event(status))
                return
            if status != STATUS_QUEUED:
                # No row, or a state this runner cannot start from (a task
                # parked for input is resumed through a fresh task id).
                logger.warning(
                    "Task %s cannot start from state %r", self.task_id, status,
                )
                await self._fail_before_start("Task state record not found.")
                return

            claimed = await self._safe_mark(
                lambda repo: repo.mark_running(self.task_id),
            )
            if not claimed:
                status = await self._read_status()
                if status == STATUS_CANCELLING:
                    await self._safe_mark(
                        lambda repo: repo.mark_cancelled(self.task_id),
                    )
                    self.session.push(_CANCELLED_EVENT)
                elif status in TERMINAL_STATUSES:
                    self.session.push(_terminal_event(status))
                else:
                    await self._fail_before_start(
                        "Task was superseded before it started.",
                    )
                return

            source = self._source_factory(self.session.cancel_event)
            async for event in source:
                self.session.push(event)
                event_type = event["type"]
                if event_type in TERMINAL_EVENT_TYPES:
                    if terminal_seen is None:
                        terminal_seen = event_type
                    if event_type == SSE_ERROR and error_message is None:
                        error_message = event["data"].get("error", "")

            # The source completed cleanly: the subscriber must never be
            # handed an invented error frame, whatever the database does —
            # finalization contains its own failures. The await stays inside
            # the try because exceptions raised in an ``else`` block bypass
            # the handlers below, and a cancel landing during finalization
            # must be finalized by the handler, not bypass it.
            await self._finalize_after_stream(terminal_seen, error_message)
        except asyncio.CancelledError:
            # Shutdown cancelled the runner mid-flight: finalize where the row
            # is still active, surface the terminal frame, then propagate so
            # the awaiting shutdown gather sees the cancellation.
            await self._finalize_cancelled_run(terminal_seen)
            raise
        except Exception as exc:
            logger.error("Task %s failed: %s", self.task_id, exc, exc_info=True)
            message = str(exc)
            try:
                await self._safe_mark(
                    lambda repo: repo.mark_failed(self.task_id, message),
                )
                try:
                    final = await self._read_status()
                except Exception:
                    final = None
                if final == STATUS_CANCELLED:
                    # mark_failed is cancel-aware: the user's cancel intent
                    # outranks this error, so surface cancellation, not failure.
                    if terminal_seen != SSE_CANCELLED:
                        self.session.push(_CANCELLED_EVENT)
                elif terminal_seen != SSE_ERROR:
                    self.session.push(make_event(SSE_ERROR, {"error": str(exc)}))
            except asyncio.CancelledError:
                # A hard cancel landing inside the error wind-down must go
                # through the same finalization as a cancel landing mid-flight,
                # or the row would stay runnable with no runner left.
                await self._finalize_cancelled_run(terminal_seen)
                raise
        finally:
            watchdog = _cancel_watchdogs.pop(self.task_id, None)
            if watchdog is not None and watchdog is not asyncio.current_task():
                watchdog.cancel()
            self.session.finish()
            stream_hub.remove(self.task_id, self.session)
            _tasks.pop(self.task_id, None)

    async def _finalize_after_stream(
        self, terminal_seen: Optional[str], error_message: Optional[str],
    ) -> None:
        """Finalize durable state after the source completed cleanly.

        Every database failure here is contained: reads degrade to ``None``
        and writes go through ``_safe_mark``'s retries, so a database hiccup
        downgrades to best-effort finalization from the events the source
        reported instead of closing a clean stream with an error frame.
        """
        try:
            status = await self._read_status()
        except Exception:
            logger.warning(
                "Status read failed for task %s", self.task_id, exc_info=True,
            )
            status = None
        if status == STATUS_AWAITING_INPUT:
            # The loop parked the task for user input; the checkpoint row
            # must survive untouched. The done frame only closes streams.
            self.session.push(make_event(SSE_DONE, {}))
            return
        if terminal_seen == SSE_DONE:
            # The done frame was already delivered: the subscriber saw a
            # complete reply, so the completion is the real outcome and a
            # cancel landing during finalization cannot change it. The write
            # must land completed even over a cancelling row, and no frame
            # can follow the delivered terminal.
            await self._safe_mark(lambda repo: repo.mark_completed(self.task_id))
            return
        if (
            status == STATUS_CANCELLING
            or terminal_seen == SSE_CANCELLED
            or (terminal_seen is None and self.session.cancel_event.is_set())
        ):
            # Cancellation owns the wind-down: the durable row ends cancelled
            # even when the cancel intent was never recorded (shutdown sets
            # the cancel events without a database write), and a cancelled
            # stream is never recorded as a completion.
            await self._safe_mark(lambda repo: repo.mark_cancelled(self.task_id))
            if terminal_seen != SSE_CANCELLED:
                self.session.push(_CANCELLED_EVENT)
            return
        if terminal_seen == SSE_ERROR:
            # The source already pushed the error frame; only the durable
            # status still needs finalizing.
            await self._safe_mark(
                lambda repo: repo.mark_failed(
                    self.task_id, error_message or "Task failed",
                ),
            )
            return
        await self._safe_mark(lambda repo: repo.mark_completed(self.task_id))
        if terminal_seen is None:
            self.session.push(make_event(SSE_DONE, {}))

    async def _finalize_cancelled_run(self, terminal_seen: Optional[str]) -> None:
        """Finalize a hard-cancelled run and push the frame closing its stream.

        The shield keeps the finalization alive across a second cancel (the
        shutdown grace window expiring): the wait aborts while the finalize
        keeps running to completion, leaving the outcome genuinely unknown
        when the frame is chosen. With no terminal delivered yet,
        ``cancelled`` is pushed only on a decision the finalization actually
        made; ``parked`` and ``unknown`` close the stream with ``done``,
        which asserts no task state — it is the park path's normal wait-end,
        and a parked interaction stays recoverable through the active-task
        lookup — so the frame can never contradict the durable row whatever
        the still-running finalization lands.
        """
        try:
            decision = await asyncio.shield(
                self._finalize_interrupted(terminal_seen),
            )
        except asyncio.CancelledError:
            logger.warning(
                "Finalize of task %s raced a second cancel", self.task_id,
            )
            decision = "unknown"
        if terminal_seen is None:
            # The decision picks the first terminal the subscriber sees; a
            # delivered terminal is never followed by another frame.
            if decision == "cancelled":
                self.session.push(_CANCELLED_EVENT)
            else:
                self.session.push(make_event(SSE_DONE, {}))

    async def _finalize_interrupted(self, terminal_seen: Optional[str]) -> str:
        """Finalize a hard-cancelled run and report the decision made.

        Returns ``"parked"`` when the row is a live interaction checkpoint
        that must survive untouched, ``"completed"`` when the done frame was
        already delivered so the run finalizes as completed, ``"cancelled"``
        when the row was finalized as cancelled, and ``"unknown"`` when the
        row's status could not be read or names no state this runner may
        touch. The caller picks the terminal frame from the decision so the
        stream never contradicts the durable row. Never raises.
        """
        try:
            status = await self._read_status()
        except Exception:
            logger.warning(
                "Status read failed while cancelling task %s",
                self.task_id, exc_info=True,
            )
            status = None
        if status == STATUS_AWAITING_INPUT:
            return "parked"
        if status in ACTIVE_STATUSES:
            if terminal_seen == SSE_DONE:
                # The done frame was already delivered: the subscriber saw a
                # complete reply, so a cancel landing during finalization
                # cannot change the outcome. mark_completed's guarded write
                # supersedes a cancelling row.
                await self._safe_mark(lambda repo: repo.mark_completed(self.task_id))
                return "completed"
            await self._safe_mark(lambda repo: repo.mark_cancelled(self.task_id))
            return "cancelled"
        return "unknown"

    async def _fail_before_start(self, message: str) -> None:
        """Fail a task that never reached its running transition."""
        logger.warning("Task %s rejected before start: %s", self.task_id, message)
        await self._safe_mark(lambda repo: repo.mark_failed(self.task_id, message))
        self.session.push(make_event(SSE_ERROR, {"error": message}))

    async def _read_status(self) -> Optional[str]:
        async with UnitOfWork(self.project_id, allow_inactive=True) as uow:
            return await uow.task_state.get_status(self.task_id)

    async def _safe_mark(
        self, transition: Callable[[object], Awaitable[object]],
    ) -> object:
        """Apply a state transition, retrying transient database failures.

        Callers push the matching terminal frame afterwards regardless of the
        outcome, so a subscriber is always released even when SQLite stays
        unwritable. Never raises: a finalize that exhausts its attempts leaves
        the row runnable with no runner behind it, where the periodic
        stranded-row sweep (or, after a restart, the startup reconciliation)
        finalizes it.
        """
        for attempt in range(1, FINALIZE_ATTEMPTS + 1):
            try:
                async with UnitOfWork(
                    self.project_id, allow_inactive=True,
                ) as uow:
                    result = await transition(uow.task_state)
                return result
            except Exception:
                if attempt >= FINALIZE_ATTEMPTS:
                    logger.warning(
                        "Task state update failed for %s after %d attempts",
                        self.task_id, FINALIZE_ATTEMPTS, exc_info=True,
                    )
                    return None
                await asyncio.sleep(FINALIZE_RETRY_DELAY_SECONDS)

async def startup_reconcile() -> int:
    """Finalize runnable rows left behind by a run that did not survive a restart.

    Idempotent: with no runnable or consuming rows the sweep is a no-op.
    ``awaiting_input`` and ``interaction_failed`` rows are recoverable
    checkpoints and are never touched. An ``interaction_consuming`` row is an
    interrupted claim and becomes ``interaction_failed`` while retaining its
    checkpoint identity. Cancelling rows finalize as cancelled (their cancel intent is preserved);
    queued/running rows finalize as failed with the restart reason. Returns
    the number of rows finalized.
    """
    finalized = 0
    for project_id in iter_project_ids():
        try:
            async with UnitOfWork(project_id) as uow:
                finalized += await uow.task_state.fail_consuming_interactions(
                    "Interaction was interrupted before completion; retry the response."
                )
                counts = await uow.task_state.fail_all_runnable(
                    RESTART_ERROR_MESSAGE,
                )
                finalized += counts["failed"] + counts["cancelled"]
        except Exception:
            logger.warning(
                "Startup reconciliation failed for project %s",
                project_id, exc_info=True,
            )
    if finalized:
        logger.info(
            "Startup reconciliation finalized %d interrupted task(s)", finalized,
        )
    return finalized


async def wait_for_project(project_id: str, timeout: float = SHUTDOWN_GRACE_SECONDS) -> bool:
    """Drain project runners, hard-cancelling and isolating stragglers."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        active = [
            task for task_id, task in list(_tasks.items())
            if task_id and not task.done()
            and stream_hub.get(task_id) is not None
            and stream_hub.get(task_id).project_id == project_id
        ]
        if not active:
            return True
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            for task in active:
                task.cancel()
            _, pending = await asyncio.wait(active, timeout=SHUTDOWN_GRACE_SECONDS)
            return not pending
        await asyncio.wait(active, timeout=remaining)


async def wait_for_task(task_id: str, timeout: float = SHUTDOWN_GRACE_SECONDS) -> bool:
    """Wait until one runner has actually exited; never fake completion."""
    task = _tasks.get(task_id)
    if task is None or task.done():
        return True
    _, pending = await asyncio.wait([task], timeout=timeout)
    if pending:
        task.cancel()
        _, pending = await asyncio.wait([task], timeout=SHUTDOWN_GRACE_SECONDS)
        return not pending
    return True


def start_stranded_sweep() -> None:
    """Start the periodic stranded-row sweep loop.

    Idempotent: a second call while the loop runs is a no-op.
    """
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_stranded_sweep_loop())


async def stop_stranded_sweep() -> None:
    """Cancel the stranded-row sweep and wait for the current pass to end."""
    global _sweep_task
    task = _sweep_task
    if task is None:
        return
    _sweep_task = None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _stranded_sweep_loop() -> None:
    while True:
        try:
            await _sweep_stranded_rows()
            from app.services.file_deletion_service import file_deletion_service
            for project_id in iter_project_ids():
                await file_deletion_service.recover_deletions(project_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Stranded task sweep failed", exc_info=True)
        await asyncio.sleep(STRANDED_SWEEP_INTERVAL_SECONDS)


async def _sweep_stranded_rows() -> int:
    """Finalize runnable rows whose runner is gone while the process lives.

    A row is stranded when its ``updated_at`` predates the grace window and
    no in-process runner exists for its task id. The grace window covers the
    claim window: a row just inserted or just claimed has a fresh
    ``updated_at``, so a launch still in flight is never swept. Cancelling
    rows finalize as cancelled; queued/running rows as failed — the same
    cancel-aware finalization the startup reconciliation applies. Returns
    the number of rows finalized.
    """
    cutoff = (utcnow() - timedelta(seconds=STRANDED_GRACE_SECONDS)).strftime(
        '%Y-%m-%d %H:%M:%S',
    )
    finalized = 0
    for project_id in iter_project_ids():
        try:
            async with UnitOfWork(project_id) as uow:
                for row in await uow.task_state.get_stale_runnable(cutoff):
                    task_id = row["task_id"]
                    if task_id in _tasks or stream_hub.get(task_id) is not None:
                        continue
                    if row["status"] == STATUS_CANCELLING:
                        await uow.task_state.mark_cancelled(task_id)
                    else:
                        await uow.task_state.mark_failed(
                            task_id, STRANDED_ERROR_MESSAGE,
                        )
                    finalized += 1
        except Exception:
            logger.warning(
                "Stranded task sweep failed for project %s",
                project_id, exc_info=True,
            )
    if finalized:
        logger.info("Stranded-row sweep finalized %d task(s)", finalized)
    return finalized


def launch(
    *,
    task_id: str,
    project_id: str,
    source_factory: Callable[[asyncio.Event], AsyncIterator[dict]],
) -> None:
    """Start a task on the running event loop and register its stream.

    Must be called while an event loop is running (from a request handler).
    """
    session = stream_hub.create(task_id, project_id)
    runner = TaskRunner(
        task_id=task_id,
        project_id=project_id,
        source_factory=source_factory,
        session=session,
    )
    _tasks[task_id] = asyncio.create_task(runner.run())


def cancel(task_id: str) -> bool:
    """Signal cooperative cancellation for one task.

    Returns True when an active session exists for the task.
    """
    cancelled = stream_hub.cancel_task(task_id)
    if cancelled:
        _schedule_cancel_watchdog(task_id)
    return cancelled


def cancel_project(project_id: str) -> int:
    """Signal cooperative cancellation for every active task of a project."""
    cancelled = stream_hub.cancel_project(project_id)
    for task_id in tuple(_tasks):
        session = stream_hub.get(task_id)
        if session is not None and session.project_id == project_id:
            _schedule_cancel_watchdog(task_id)
    return cancelled


def _schedule_cancel_watchdog(task_id: str) -> None:
    task = _tasks.get(task_id)
    if task is None or task.done():
        return
    existing = _cancel_watchdogs.get(task_id)
    if existing is not None and not existing.done():
        return
    _cancel_watchdogs[task_id] = asyncio.create_task(
        _hard_cancel_after_grace(task_id, task),
    )


async def _hard_cancel_after_grace(task_id: str, expected: asyncio.Task) -> None:
    try:
        await asyncio.sleep(SHUTDOWN_GRACE_SECONDS)
        task = _tasks.get(task_id)
        if task is expected and not task.done():
            task.cancel()
    finally:
        if _cancel_watchdogs.get(task_id) is asyncio.current_task():
            _cancel_watchdogs.pop(task_id, None)


async def shutdown_all() -> None:
    """Cooperatively cancel every runner, then hard-cancel stragglers.

    Each session's cancel event is set first so sources can wind down and
    finalize their own state; a runner that saw the signal finalizes its row
    as cancelled — shutdown deliberately records no cancel intent in the
    database, and the runner-side finalization is what keeps the durable
    status truthful. After the grace window any runner still alive is
    cancelled; its shielded finalization still lands, and only a finalize
    that cannot complete at all is left to the stranded-row sweep (which
    startup reconciliation also covers across a restart).

    A task launched while the drain above was running missed the cooperative
    signal, so a final registry pass sets its cancel event and cancels it
    too.
    """
    snapshot_ids = set(_tasks)
    for task_id in snapshot_ids:
        stream_hub.cancel_task(task_id)
    running = [
        task for task_id, task in _tasks.items()
        if task_id in snapshot_ids and not task.done()
    ]
    if running:
        _, pending = await asyncio.wait(running, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    late = [
        (task_id, task)
        for task_id, task in _tasks.items()
        if task_id not in snapshot_ids and not task.done()
    ]
    for task_id, task in late:
        stream_hub.cancel_task(task_id)
        task.cancel()
    if late:
        await asyncio.gather(*(task for _, task in late), return_exceptions=True)
    watchdogs = list(_cancel_watchdogs.values())
    _cancel_watchdogs.clear()
    for watchdog in watchdogs:
        watchdog.cancel()
    if watchdogs:
        await asyncio.gather(*watchdogs, return_exceptions=True)
