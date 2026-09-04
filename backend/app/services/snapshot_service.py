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

from sqlalchemy.exc import SQLAlchemyError

from app.core.exceptions import DatabaseException
from app.core.logging import get_logger
from app.core.utils import utcnow, parse_iso
from app.services.git_service import (
    DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB,
    git_service,
    parse_max_new_file_mb,
)
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


def _outcome_with_skipped(outcome: dict, result: dict) -> dict:
    """Attach size-capped exclusions to a snapshot outcome.

    Without this the auto pipeline collapses every clean result to a bare
    status, and files that may sit unprotected forever (only excluded work
    pending, so no commit ever records them) leave no trace beyond logs.
    """
    skipped = result.get("skipped_large_files")
    if skipped:
        outcome["skipped_large_files"] = skipped
    return outcome


class SnapshotService:
    """Checks snapshot config and auto-commits on file changes."""

    def __init__(self) -> None:
        # One pending trailing re-check per project. Entries are released by
        # a done-callback when the task finishes, so completed tasks never
        # accumulate. Bookkeeping is process-local only; correctness never
        # depends on it because the fire-time re-check reads live state.
        self._pending: Dict[str, asyncio.Task] = {}

    async def get_max_new_file_mb(self, project_id: str) -> int:
        """Read the per-project cap, falling back safely on DB failure."""
        try:
            async with UnitOfWork(project_id) as uow:
                raw = await uow.config.get(
                    "snapshot_max_new_file_mb",
                    str(DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB),
                )
            return parse_max_new_file_mb(raw)
        except (DatabaseException, SQLAlchemyError, OSError):
            logger.warning(
                "Failed to read snapshot file-size limit for %s; using %d MiB",
                project_id,
                DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB,
                exc_info=True,
            )
            return DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB

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
                max_file_mb = parse_max_new_file_mb(
                    await uow.config.get(
                        "snapshot_max_new_file_mb",
                        str(DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB),
                    )
                )

            # 2. History check + commit run in a thread: git subprocesses must
            #    never block the event loop.
            outcome = await asyncio.to_thread(
                self._snapshot_if_due, project_id, interval_min, max_file_mb)
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

    def _snapshot_if_due(
        self,
        project_id: str,
        interval_min: float,
        max_new_file_mb: int = DEFAULT_SNAPSHOT_MAX_NEW_FILE_MB,
    ) -> dict:
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
                project_id,
                defer_unstable=True,
                max_new_file_bytes=max_new_file_mb * 1024 * 1024,
            )
            if result.get("success") is False and result.get("reason") == "deferred":
                logger.info("Auto-snapshot deferred for %s: %s",
                            project_id, result.get("detail"))
                return {"status": "deferred"}
            if result.get("success") is False:
                logger.debug(f"Auto-snapshot skipped (nothing to commit) for {project_id}")
                return _outcome_with_skipped({"status": "noop"}, result)
            logger.info(f"Auto-snapshot committed for {project_id}: {result.get('commit', '?')}")
            return _outcome_with_skipped({"status": "committed"}, result)
        except Exception as e:
            logger.warning("Auto-snapshot commit failed for project %s: %s", project_id, e, exc_info=True)
            return {"status": "error", "error": str(e)}

    async def commit_now(self, project_id: str) -> dict:
        """Manual commit-now entry for the route.

        Logging and health reporting happen inside the worker thread: the
        HTTP request may be cancelled while git is still running, and that
        cancellation must never swallow the outcome.
        """
        max_file_mb = await self.get_max_new_file_mb(project_id)

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
                result = git_service.create_snapshot_commit(
                    project_id,
                    max_new_file_bytes=max_file_mb * 1024 * 1024,
                )
            except Exception as e:
                logger.warning("Manual snapshot failed for %s: %s",
                               project_id, e, exc_info=True)
                report({"status": "error", "error": str(e)})
                raise
            if result.get("success") is False:
                logger.debug("Manual snapshot skipped (nothing to commit) for %s", project_id)
                # A clean noop still proves the pipeline works; report it so
                # a stale failure banner clears even when nothing was pending.
                report(_outcome_with_skipped({"status": "noop"}, result))
            else:
                logger.info("Manual snapshot committed for %s: %s",
                            project_id, result.get("commit"))
                report(_outcome_with_skipped({"status": "committed"}, result))
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

        Failures accumulate until the pipeline runs clean again — a commit
        or a noop (nothing pending, with the size cap a project can rest in
        that state indefinitely). A deferral (files still being written) is
        normal during active work and must not trip the failure banner.

        Clean outcomes also mirror the size-capped files that stayed
        unprotected, so the UI can list them. Deferred and failed attempts
        run no size scan, so they keep the previous mirror.
        """
        status = outcome.get("status", "error")
        if status == "noop":
            status = "ok"
        try:
            async with UnitOfWork(project_id) as uow:
                raw = await uow.config.get(_HEALTH_KEY, "")
                prev = json.loads(raw) if raw else {}
                failures = prev.get("consecutive_failures", 0)
                if status == "error":
                    failures += 1
                elif status in ("committed", "ok"):
                    failures = 0
                now_iso = utcnow().isoformat()
                health = {
                    "status": status,
                    "consecutive_failures": failures,
                    "last_attempt_at": now_iso,
                    "last_success_at": (now_iso if status == "committed"
                                        else prev.get("last_success_at")),
                    "last_error": outcome.get("error"),
                    "pending_skipped_large_files": prev.get(
                        "pending_skipped_large_files"),
                }
                if status in ("committed", "ok"):
                    skipped = outcome.get("skipped_large_files") or []
                    # Full list with sizes: the UI renders every name and
                    # how much it weighs in its clickable list.
                    health["pending_skipped_large_files"] = (
                        {"count": len(skipped), "files": skipped,
                         "at": now_iso}
                        if skipped else None
                    )
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
            "pending_skipped_large_files": health.get(
                "pending_skipped_large_files"),
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
