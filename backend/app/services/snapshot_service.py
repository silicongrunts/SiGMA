"""
Auto-snapshot service — checks config and auto-commits after file mutations.

Called by file_service after every file create/write/delete/rename/upload.
All errors are caught and logged — never propagates to the caller.

A mutation skipped because the minimum interval has not elapsed is not lost:
a one-shot trailing timer re-runs the check when the interval expires, so the
final state of a burst of edits is committed even if the user never saves
again. Timers live only in this process; a restart drops them, and the next
project open or file save re-checks the pending work.
"""
import asyncio
import functools
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

            # 2. Check time since last commit
            try:
                commits = git_service.get_log(project_id, 1)
                if commits:
                    last_date_str = commits[0].get("date", "")
                    if last_date_str:
                        last_date = parse_iso(last_date_str)
                        elapsed_min = (utcnow() - last_date).total_seconds() / 60.0
                        if elapsed_min < interval_min:
                            logger.debug(f"Auto-snapshot skipped (elapsed={elapsed_min:.1f}m < interval={interval_min}m) for {project_id}")
                            self._arm_trailing_snapshot(project_id, interval_min, elapsed_min)
                            return
            except Exception:
                logger.debug("Failed to read commit history for auto-snapshot", exc_info=True)

            # 3. Stage all and commit
            self._auto_commit(project_id)
        except Exception as e:
            logger.warning("Auto-snapshot failed for project %s: %s", project_id, e, exc_info=True)

    def _arm_trailing_snapshot(self, project_id: str, interval_min: float, elapsed_min: float) -> None:
        """Arm a one-shot re-check at last-commit + interval, one per project."""
        existing = self._pending.get(project_id)
        if (existing is not None and not existing.done()
                and not existing.get_loop().is_closed()):
            return
        remaining = (interval_min - elapsed_min) * 60.0
        delay_sec = min(max(remaining, _MIN_TIMER_DELAY_SEC), interval_min * 60.0)
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

    def _auto_commit(self, project_id: str) -> None:
        """Stage all changes and commit (sync). Caller must guarantee config permits this."""
        try:
            result = git_service.create_snapshot_commit(project_id)
            if result.get("success") is False:
                logger.debug(f"Auto-snapshot skipped (nothing to commit) for {project_id}")
            else:
                logger.info(f"Auto-snapshot committed for {project_id}: {result.get('commit', '?')}")
        except Exception as e:
            logger.warning("Auto-snapshot commit failed for %s: %s", project_id, e, exc_info=True)


snapshot_service = SnapshotService()
