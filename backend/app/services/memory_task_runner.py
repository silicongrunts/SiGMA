"""Dedicated single-flight runner for the background memory queue.

Mirrors ``LibraryTaskRunner``'s claim/heartbeat/finalize protocol but claims
only ``QUEUE_MEMORY`` with concurrency 1, so a memory extraction never waits
behind a large document conversion and never runs two memory tasks at once
(the queue's dedupe key already merges duplicates; this is the second belt
on the same braces). Terminal outcomes notify ``memory_service`` so the
depletion counters reload and the project's slot frees for the next cycle.
"""

from __future__ import annotations

import asyncio
import json

from app.core.logging import get_logger
from app.core.task_status import STATUS_FAILED
from app.database.unit_of_work import UnitOfWork
from app.services.background_task_service import project_allows_persistence
from app.services.library_task_protocol import QUEUE_MEMORY, get_task_handler

logger = get_logger(__name__)

_WAKE_IDLE_TIMEOUT_SECONDS = 30.0


class MemoryTaskRunner:
    """Single-consumer loop over the memory background queue."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._owner = f"memory_runner_{id(self):x}"
        # Set by ``stop`` so the loop can tell a shutdown cancellation from a
        # handler-side one (project deleted mid-run, lease lost) — the latter
        # must not end the runner.
        self._stopping = False
        # Project of the single in-flight ``_run_one``, and its handler's
        # cancel event — the integration points for project deletion (see
        # ``cancel_project`` / ``wait_for_project``).
        self._active_project: str | None = None
        self._cancel_events_by_project: dict[str, set[asyncio.Event]] = {}

    @property
    def owner(self) -> str:
        return self._owner

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._safe_run())
        logger.info("Memory background task runner started")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stopping = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._stopping = False
        logger.info("Memory background task runner stopped")

    async def _safe_run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Memory background runner stopped with error: %s", exc)

    async def _run(self) -> None:
        from app.core.config import settings
        from app.services.background_task_service import background_task_service

        self._refresh_known_projects()

        while True:
            wake_event = background_task_service.wake_event
            try:
                await asyncio.wait_for(
                    wake_event.wait(), timeout=_WAKE_IDLE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                pass  # idle tick: re-check for lease-expired work
            wake_event.clear()
            for project_id in _projects_with_memory_rows():
                try:
                    async with UnitOfWork(project_id) as uow:
                        task = await uow.background_tasks.claim_next(
                            queue=QUEUE_MEMORY,
                            owner=self.owner,
                            lease_seconds=settings.BACKGROUND_TASK_LEASE_SECONDS,
                        )
                except Exception:
                    logger.warning(
                        "Failed to claim memory task for %s", project_id, exc_info=True,
                    )
                    if not project_allows_persistence(project_id):
                        # The project is gone; stop offering it for claims.
                        _known_memory_projects.discard(project_id)
                    continue
                if task is None:
                    continue
                if task.status == STATUS_FAILED:
                    await self._handle_failed_claim(project_id, task)
                    continue
                try:
                    await self._run_one(project_id, task)
                except asyncio.CancelledError:
                    if self._stopping:
                        raise
                    # Handler-side cancellation: the row is already
                    # finalized; the single-flight loop lives on.
                    logger.info(
                        "Memory task for %s cancelled mid-run; runner continues",
                        project_id,
                    )
                except Exception:
                    # Safety net: _run_one finalizes and logs its own
                    # failures — the loop must survive either way.
                    logger.exception(
                        "Memory task for %s crashed past finalize; "
                        "runner continues", project_id,
                    )
                break

    async def _run_one(self, project_id: str, task) -> None:
        from app.core.config import settings
        from app.services.library_task_protocol import RunningTaskContext

        owner = task.lease_owner
        ctx = RunningTaskContext(
            project_id=project_id,
            task_id=task.id,
            owner=owner,
            lease_seconds=settings.BACKGROUND_TASK_LEASE_SECONDS,
        )
        self._active_project = project_id
        self._cancel_events_by_project.setdefault(project_id, set()).add(ctx.cancel_event)
        heartbeat_task: asyncio.Task | None = None
        cancelled = False
        failure: str | None = None
        try:
            try:
                payload = json.loads(task.payload_json or "{}")
                handler = get_task_handler(task.kind)
                if handler is None:
                    raise RuntimeError(f"Unknown memory background task kind: {task.kind}")
                heartbeat_task = asyncio.create_task(self._heartbeat_while_running(ctx))
                await handler(ctx, payload)
            except asyncio.CancelledError:
                cancelled = True
            except Exception as exc:
                logger.exception("Memory background task %s failed", task.id)
                failure = str(exc)
            finally:
                events = self._cancel_events_by_project.get(project_id)
                if events is not None:
                    events.discard(ctx.cancel_event)
                    if not events:
                        self._cancel_events_by_project.pop(project_id, None)
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)

            await self._finalize(
                project_id, task, owner, cancelled=cancelled, failure=failure,
            )
        finally:
            # Cleared only after finalize: wait_for_project must not report a
            # project drained while its final DB write is still in flight
            # (project deletion must not outrun a late write).
            self._active_project = None
        if cancelled:
            # Propagate so ``stop`` can cancel the loop; a handler-side
            # cancellation is caught one level up in ``_run`` instead.
            raise asyncio.CancelledError

    async def _finalize(
        self, project_id: str, task, owner: str, *,
        cancelled: bool, failure: str | None,
    ) -> None:
        """Write the terminal row state, then free the project's memory slot.

        Guarded on purpose: the project database may vanish (project deleted
        mid-run) and the finalize write may itself fail — neither may kill
        the runner. A failed finalize leaves the row to its lease-expiry
        reclaim and keeps the slot held; a vanished project frees the slot
        outright (its queue dies with the database).
        """
        from app.services.memory_service import memory_service

        kind = _payload_kind(task)
        session_id = _payload_session(task)
        if not project_allows_persistence(project_id):
            logger.info(
                "Memory task %s skipped finalize: project %s no longer persists",
                task.id, project_id,
            )
            await memory_service.on_memory_task_finished(project_id, kind, session_id)
            return
        try:
            async with UnitOfWork(project_id, allow_inactive=True) as uow:
                if cancelled:
                    if not await uow.background_tasks.mark_cancelled(task.id, owner):
                        # Same contract as the failure branch below: a
                        # scheduled retry owns the outcome and keeps the
                        # slot held.
                        outcome = await uow.background_tasks.mark_failed_or_retry(
                            task.id, owner, "Task cancelled",
                        )
                        if outcome == "queued":
                            return
                elif failure is None:
                    if await uow.background_tasks.is_cancelling(task.id):
                        await uow.background_tasks.mark_cancelled(task.id, owner)
                    elif not await uow.background_tasks.mark_completed(task.id, owner):
                        logger.debug(
                            "Memory task %s was no longer completable at finalize", task.id,
                        )
                else:
                    # Status string from the repo: "queued" schedules a retry
                    # that owns the outcome (slot stays held); "failed" is
                    # terminal and "missing" means the row is no longer ours —
                    # both free the project's slot.
                    outcome = await uow.background_tasks.mark_failed_or_retry(
                        task.id, owner, failure,
                    )
                    if outcome == "queued":
                        return
        except Exception:
            logger.warning("Memory task %s finalize failed", task.id, exc_info=True)
            return
        await memory_service.on_memory_task_finished(project_id, kind, session_id)

    async def _handle_failed_claim(self, project_id: str, task) -> None:
        """Handle a row claim_next already marked failed.

        Repeated lease expiries let the queue write the task off at claim
        time and still return the row; its handler must never run (mirrors
        the library runner's final-failure path). Releasing the slot is all
        that is left, so the counters reload for the next cycle.
        """
        from app.services.memory_service import memory_service

        logger.warning(
            "Memory task %s was already failed at claim; freeing slot", task.id,
        )
        await memory_service.on_memory_task_finished(
            project_id, _payload_kind(task), _payload_session(task),
        )

    def cancel_project(self, project_id: str) -> None:
        """Signal every live memory handler of *project_id* to stop."""
        for event in tuple(self._cancel_events_by_project.get(project_id, ())):
            event.set()

    async def wait_for_project(self, project_id: str, timeout: float = 10.0) -> bool:
        """Wait until no in-flight memory task belongs to *project_id*.

        Finalize included: a project counts as drained only after its last
        task's terminal DB write has landed.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while self._active_project == project_id:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                self.cancel_project(project_id)
                return False
            await asyncio.sleep(min(0.05, remaining))
        return True

    def _refresh_known_projects(self) -> None:
        """Seed the project set at startup so pre-restart queued rows recover.

        claim_next is per-project (each project is its own SQLite DB) and the
        queue has no cross-project index; without this seeding, rows queued
        before a restart would sit unclaimed forever.
        """
        try:
            from app.core.project_registry import iter_project_ids

            _known_memory_projects.update(iter_project_ids())
        except Exception:
            logger.warning("Failed to enumerate projects for memory runner", exc_info=True)

    async def _heartbeat_while_running(self, ctx) -> None:
        """Renew the lease often enough to outlive it (mirrors the library
        runner): never more than every sixty seconds, never less than ten,
        and comfortably inside the lease window."""
        interval = min(60.0, max(10.0, ctx.lease_seconds // 3))
        while True:
            await asyncio.sleep(interval)
            # ctx.heartbeat() swallows its own errors; False means the lease
            # is lost or the row is cancelling.
            if not await ctx.heartbeat():
                # Lease lost (or the task is cancelling) — the handler
                # must stop; signal it through the shared cancel event.
                ctx.cancel_event.set()
                return


def _payload_kind(task) -> str:
    try:
        return (json.loads(task.payload_json or "{}") or {}).get("task", "")
    except ValueError:
        return ""


def _payload_session(task) -> str:
    """The session a queued extraction window belongs to ("other" kinds carry "")."""
    try:
        return (json.loads(task.payload_json or "{}") or {}).get("session_id", "")
    except ValueError:
        return ""


def _projects_with_memory_rows() -> list[str]:
    """Projects this process has seen enqueue/track memory work for.

    claim_next is per-project (each project is its own SQLite DB), and the
    queue has no cross-project index. Rather than scanning every registered
    project on every tick, the runner keeps the set of projects it has seen
    enqueue or restart-reconcile memory rows for.
    """
    return sorted(_known_memory_projects)


_known_memory_projects: set[str] = set()


def note_memory_project(project_id: str) -> None:
    """Register a project as having (potentially) memory queue rows."""
    _known_memory_projects.add(project_id)


# Single process-wide instance: lifecycle starts/stops it, project deletion
# drains it, and memory_service registers projects with it.
memory_task_runner = MemoryTaskRunner()
