"""
Auto-snapshot service — checks config and auto-commits after file mutations.

Called by file_service after every file create/write/delete/rename/upload.
All errors are caught and logged — never propagates to the caller.
"""
from app.core.logging import get_logger
from app.core.utils import utcnow, parse_iso
from app.services.git_service import git_service
from app.database.unit_of_work import UnitOfWork
from app.core.atomic_file import ProjectFileLock

logger = get_logger(__name__)


class SnapshotService:
    """Checks snapshot config and auto-commits on file changes."""

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
                            return
            except Exception:
                logger.debug("Failed to read commit history for auto-snapshot", exc_info=True)

            # 3. Stage all and commit
            self._auto_commit(project_id)
        except Exception as e:
            logger.warning("Auto-snapshot failed for project %s: %s", project_id, e, exc_info=True)

    def _auto_commit(self, project_id: str) -> None:
        """Stage all changes and commit (sync). Caller must guarantee config permits this."""
        try:
            project_path = git_service.get_project_path(project_id)
            with ProjectFileLock(project_path / ".git" / "index"):
                git_service.stage_all(project_id)
                message = git_service.build_staged_snapshot_message(project_id)
                result = git_service.commit(project_id, message)
            if result.get("success") is False:
                logger.debug(f"Auto-snapshot skipped (nothing to commit) for {project_id}")
            else:
                logger.info(f"Auto-snapshot created: {message} ({result.get('commit', '?')}) for {project_id}")
        except Exception as e:
            logger.warning("Auto-snapshot commit failed for %s: %s", project_id, e, exc_info=True)


snapshot_service = SnapshotService()
