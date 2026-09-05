import posixpath

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert

from app.core.exceptions import FileSystemError
from app.database.models import (
    Annotation, AnnotationFileState, AnnotationFileTransaction, FileDeletion,
    TaskState,
)


def _matches(path: str, parent: str, is_directory: bool) -> bool:
    path = posixpath.normpath(path).lstrip("/")
    parent = posixpath.normpath(parent).lstrip("/")
    return path == parent or (is_directory and path.startswith(parent + "/"))


class FileDeletionRepository:
    def __init__(self, session):
        self._session = session

    async def get_pending(self) -> list[dict]:
        result = await self._session.execute(select(FileDeletion))
        return [
            {"path": row.path, "is_directory": row.is_directory}
            for row in result.scalars()
        ]

    async def assert_available(self, path: str) -> None:
        if await self.is_deleting(path):
            raise FileSystemError(
                "File deletion is in progress; retry the deletion before continuing.",
                code="FILE_DELETING", status_code=409,
            )

    async def is_deleting(self, path: str) -> bool:
        for pending in await self.get_pending():
            if _matches(path, pending["path"], pending["is_directory"]):
                return True
        return False

    async def stage_begin(self, path: str, is_directory: bool) -> None:
        await self._session.execute(
            insert(FileDeletion).values(path=path, is_directory=is_directory)
            .on_conflict_do_nothing(index_elements=["path"])
        )

    async def annotation_ids(self, path: str, is_directory: bool) -> list[str]:
        result = await self._session.execute(select(Annotation.id, Annotation.file_path))
        return [
            row.id for row in result
            if _matches(row.file_path, path, is_directory)
        ]

    async def stage_finish(self, path: str, is_directory: bool) -> None:
        annotation_ids = await self.annotation_ids(path, is_directory)
        await self._session.execute(delete(TaskState).where(
            TaskState.owner_type == "annotation",
            TaskState.owner_id.in_(annotation_ids),
        ))
        await self._session.execute(delete(Annotation).where(Annotation.id.in_(annotation_ids)))
        for model in (AnnotationFileState, AnnotationFileTransaction):
            result = await self._session.execute(select(model.file_path).distinct())
            paths = [
                value for value in result.scalars()
                if _matches(value, path, is_directory)
            ]
            await self._session.execute(delete(model).where(model.file_path.in_(paths)))
        await self._session.execute(delete(FileDeletion).where(FileDeletion.path == path))
