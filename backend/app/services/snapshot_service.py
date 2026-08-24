"""
Auto-snapshot service — checks config and auto-commits after file mutations.

Called by file_service after every file create/write/delete/rename/upload.
All errors are caught and logged — never propagates to the caller.

A mutation skipped because the minimum interval has not elapsed is not lost:
a one-shot trailing timer re-runs the check when the interval expires, so the
final state of a burst of edits is committed even if the user never saves
again. Timers live only in this process; a restart drops them, and the next
project open or file save re-checks the pending work.

Snapshot health (last outcome, consecutive failures) is persisted per project
so the UI can alert the user when version protection is broken instead of
failing silently.
"""
import asyncio
import functools
import json
from typing import Dict

from app.core.logging import get_logger
from app.core.utils import utcnow, parse_iso
from app.services.git_service import git_service
from app.database.unit_of_work import UnitOfWork

logger = get_logger(__name__)

# Floor for trailing-timer delays. Prevents rapid re-arm loops when the
# remaining interval is a fraction of a second or the recorded commit date
# is skewed into the future (elapsed goes negative, remaining exceeds the
# full interval).
_MIN_TIMER_DELAY_SEC = 1.0

# A deferred snapshot (files still being written) retries after this delay:
# slightly above the freshness window so the retry lands right after the
# writes settle, instead of waiting out the whole snapshot interval.
_DEFERRED_RETRY_SEC = 15.0

_HEALTH_KEY = "snapshot_health"


class SnapshotService:
    """Checks snapshot config and auto-commits on file changes."""

    def __init__(self) -> None:
        # One pending trailing re-check per project. Entries are released by
        # a done-callback when the task finishes, so completed tasks never
        # accumulate. Bookkeeping is process-local only; correctness never
        # depends on it because the fire-time re-check reads live state.
        self._pending: Dict[str, asyncio.Task] = {}

    async def maybe_snapshot(self, project_id: str) -> None:
        """Check if auto-snapshot should fire, then commit the project state."""
        try:
            # 1. Read config from project DB
            async with UnitOfWork(project_id) as uow:
                enabled = await uow.config.get("snapshot_enabled", "true")
                if enabled.lower() != "true":
                    return

                interval_str = await uow.config.get("snapshot_interval_minutes", "5")
                try:
                    interval_min = int(interval_str)
                except (ValueError, TypeError):
                    interval_min = 5
                if interval_min < 1:
                    interval_min = 1

            # 2. History check + commit run in a thread: git subprocesses must
            #    never block the event loop.
            outcome = await asyncio.to_thread(
                self._snapshot_if_due, project_id, interval_min)
            if outcome["status"] == "skipped_interval":
                remaining = (interval_min - outcome["elapsed_min"]) * 60.0
                delay = min(max(remaining, _MIN_TIMER_DELAY_SEC), interval_min * 60.0)
                self._arm_trailing_snapshot(project_id, delay)
            elif outcome["status"] == "deferred":
                self._arm_trailing_snapshot(project_id, _DEFERRED_RETRY_SEC)
            await self.record_health(project_id, outcome)
        except Exception as e:
            logger.warning("Auto-snapshot failed for project %s: %s", project_id, e, exc_info=True)
            await self.record_health(project_id, {"status": "error", "error": str(e)})

    def _snapshot_if_due(self, project_id: str, interval_min: float) -> dict:
        """Blocking snapshot check + commit, for a worker thread.

        All outcome logging happens here on purpose: the awaiting coroutine
        may be cancelled while git is still running, and that cancellation
        must never lose the error trail.
        """
        try:
            try:
                commits = git_service.get_log(project_id, 1)
                if commits:
                    last_date_str = commits[0].get("date", "")
                    if last_date_str:
                        last_date = parse_iso(last_date_str)
                        elapsed_min = (utcnow() - last_date).total_seconds() / 60.0
                        if elapsed_min < interval_min:
                            logger.debug(f"Auto-snapshot skipped (elapsed={elapsed_min:.1f}m < interval={interval_min}m) for {project_id}")
                            return {"status": "skipped_interval", "elapsed_min": elapsed_min}
            except Exception:
                logger.debug("Failed to read commit history for auto-snapshot", exc_info=True)

            result = git_service.create_snapshot_commit(
                project_id, defer_unstable=True)
            if result.get("success") is False and result.get("reason") == "deferred":
                logger.info("Auto-snapshot deferred for %s: %s",
                            project_id, result.get("detail"))
                return {"status": "deferred"}
            if result.get("success") is False:
                logger.debug(f"Auto-snapshot skipped (nothing to commit) for {project_id}")
                return {"status": "noop"}
            logger.info(f"Auto-snapshot committed for {project_id}: {result.get('commit', '?')}")
            return {"status": "committed"}
        except Exception as e:
            logger.warning("Auto-snapshot commit failed for project %s: %s", project_id, e, exc_info=True)
            return {"status": "error", "error": str(e)}

    async def commit_now(self, project_id: str) -> dict:
        """Manual commit-now entry for the route.

        Logging and health reporting happen inside the worker thread: the
        HTTP request may be cancelled while git is still running, and that
        cancellation must never swallow the outcome.
        """
        loop = asyncio.get_running_loop()

        def report(outcome: dict) -> None:
            try:
                loop.call_soon_threadsafe(
                    self._schedule_health_recording, project_id, outcome)
            except RuntimeError:
                # Loop closed mid-flight (shutdown); the log above is the
                # durable record — never let this mask the real outcome.
                pass

        def work() -> dict:
            try:
                result = git_service.create_snapshot_commit(project_id)
            except Exception as e:
                logger.warning("Manual snapshot failed for %s: %s",
                               project_id, e, exc_info=True)
                report({"status": "error", "error": str(e)})
                raise
            if result.get("success") is False:
                logger.debug("Manual snapshot skipped (nothing to commit) for %s", project_id)
            else:
                logger.info("Manual snapshot committed for %s: %s",
                            project_id, result.get("commit"))
                report({"status": "committed"})
            return result

        return await asyncio.to_thread(work)

    def _schedule_health_recording(self, project_id: str, outcome: dict) -> None:
        task = asyncio.create_task(self.record_health(project_id, outcome))

        def _consume(task: asyncio.Task) -> None:
            if not task.cancelled():
                task.exception()

        task.add_done_callback(_consume)

    async def record_health(self, project_id: str, outcome: dict) -> None:
        """Persist snapshot health per project for UI alerting.

        Failures accumulate until a commit succeeds; a deferral (files still
        being written) is normal during active work and must not trip the
        failure banner.
        """
        status = outcome.get("status", "error")
        if status == "noop":
            return
        try:
            async with UnitOfWork(project_id) as uow:
                raw = await uow.config.get(_HEALTH_KEY, "")
                prev = json.loads(raw) if raw else {}
                failures = prev.get("consecutive_failures", 0)
                if status == "error":
                    failures += 1
                elif status == "committed":
                    failures = 0
                now_iso = utcnow().isoformat()
                health = {
                    "status": status,
                    "consecutive_failures": failures,
                    "last_attempt_at": now_iso,
                    "last_success_at": (now_iso if status == "committed"
                                        else prev.get("last_success_at")),
                    "last_error": outcome.get("error"),
                }
                await uow.config.set(_HEALTH_KEY, json.dumps(health, ensure_ascii=False))
        except Exception:
            logger.debug("Failed to record snapshot health", exc_info=True)

    async def get_health(self, project_id: str) -> dict:
        """Latest persisted snapshot health; healthy defaults when absent."""
        async with UnitOfWork(project_id) as uow:
            raw = await uow.config.get(_HEALTH_KEY, "")
        health = json.loads(raw) if raw else {}
        return {
            "status": health.get("status", "ok"),
            "consecutive_failures": health.get("consecutive_failures", 0),
            "last_attempt_at": health.get("last_attempt_at"),
            "last_success_at": health.get("last_success_at"),
            "last_error": health.get("last_error"),
        }

    def _arm_trailing_snapshot(self, project_id: str, delay_sec: float) -> None:
        """Arm a one-shot snapshot re-check after *delay_sec*, one per project."""
        existing = self._pending.get(project_id)
        if (existing is not None and not existing.done()
                and not existing.get_loop().is_closed()):
            return
        task = asyncio.create_task(
            self._trailing_snapshot(project_id, delay_sec),
            name=f"trailing-snapshot:{project_id}",
        )
        self._pending[project_id] = task
        task.add_done_callback(functools.partial(self._drop_pending, project_id))

    async def _trailing_snapshot(self, project_id: str, delay_sec: float) -> None:
        """Wait out the interval, then re-run the full snapshot check."""
        try:
            await asyncio.sleep(delay_sec)
            # Drop this task from the pending map before re-checking: if a
            # newer commit has opened a new interval window meanwhile, the
            # re-check must be free to arm a fresh timer for it.
            task = asyncio.current_task()
            if self._pending.get(project_id) is task:
                del self._pending[project_id]
            await self.maybe_snapshot(project_id)
        except Exception as e:
            logger.warning("Trailing auto-snapshot failed for project %s: %s", project_id, e, exc_info=True)

    def _drop_pending(self, project_id: str, task: asyncio.Task) -> None:
        """Done-callback: release the pending entry, identity-guarded."""
        if self._pending.get(project_id) is task:
            del self._pending[project_id]

    async def shutdown(self) -> None:
        """Cancel pending trailing timers (app shutdown)."""
        tasks = [t for t in self._pending.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()


snapshot_service = SnapshotService()
