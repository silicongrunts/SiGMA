import asyncio
import shutil

from app.core.exceptions import FileMissingError, FileSystemError, TaskActiveError
from app.core.logging import get_logger
from app.database.unit_of_work import UnitOfWork
from app.services.file_service import file_service
from app.services import task_runtime


logger = get_logger(__name__)


class FileDeletionService:
    async def delete_item(self, project_id: str, path: str, *, resume: bool = False) -> None:
        root = file_service.get_project_path(project_id).resolve()
        target = file_service.safe_join(root, path)
        relative = target.relative_to(root)
        if not relative.parts or relative.parts[0] in (".SiGMA", ".git"):
            raise FileSystemError(
                "Use the project lifecycle controls to remove project storage.",
                code="PERMISSION_DENIED", status_code=403,
            )
        path = relative.as_posix()
        async with UnitOfWork(project_id, immediate=True) as uow:
            pending = next((
                item for item in await uow.file_deletions.get_pending()
                if item["path"] == path
            ), None)
            if pending is None:
                if resume:
                    return
                if not target.exists():
                    raise FileMissingError(path)
                await uow.file_deletions.assert_available(path)
                is_directory = target.is_dir()
                await uow.file_deletions.stage_begin(path, is_directory)
            else:
                is_directory = pending["is_directory"]
            annotation_ids = await uow.file_deletions.annotation_ids(path, is_directory)
            await uow.commit()

        task_ids = []
        for annotation_id in annotation_ids:
            async with UnitOfWork(project_id, immediate=True) as uow:
                active = await uow.task_state.get_active_by_owner("annotation", annotation_id)
                if active:
                    await uow.task_state.request_cancel(active["task_id"])
                    task_ids.append(active["task_id"])
        for task_id in task_ids:
            task_runtime.cancel(task_id)
        drained = await asyncio.gather(*(
            task_runtime.wait_for_task(task_id) for task_id in task_ids
        ))
        if not all(drained):
            raise TaskActiveError(task_id=task_ids[drained.index(False)])

        async with UnitOfWork(project_id, immediate=True) as uow:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            await uow.file_deletions.stage_finish(path, is_directory)
            await uow.commit()

        from app.agents.tools.read_state import read_state_cache
        read_state_cache.clear_many([f"annotation:{value}" for value in annotation_ids])
        read_state_cache.clear_under(target)

    async def recover_deletions(self, project_id: str) -> None:
        async with UnitOfWork(project_id) as uow:
            pending = await uow.file_deletions.get_pending()
        for item in pending:
            try:
                await self.delete_item(project_id, item["path"], resume=True)
            except Exception:
                logger.warning(
                    "File deletion remains pending for %s/%s",
                    project_id, item["path"], exc_info=True,
                )


file_deletion_service = FileDeletionService()
