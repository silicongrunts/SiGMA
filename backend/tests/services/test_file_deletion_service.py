import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.core.exceptions import AnnotationConflictError, FileSystemError, TaskActiveError
from app.database.repos.annotation_repo import AnnotationRepository
from app.database.repos.file_deletion_repo import FileDeletionRepository
from app.database.unit_of_work import UnitOfWork
from app.services import task_runtime
from app.services.annotation_service import annotation_service
from app.services.file_deletion_service import file_deletion_service
from app.services.file_service import file_service
from app.services.project_service import project_service


@pytest.fixture(autouse=True)
def isolated_project(project, monkeypatch):
    root = settings.USERDATA_DIR.resolve()
    monkeypatch.setattr(project_service, "USERDATA_DIR", root)
    monkeypatch.setattr(project_service, "SIGMA_DIR", root / ".SiGMA")
    monkeypatch.setattr(project_service, "PROJECTS_FILE", root / ".SiGMA" / "projects.json")
    monkeypatch.setattr(file_service, "_after_file_mutation", AsyncMock())


async def create_annotation(project, path="paper.md"):
    full_path = settings.get_project_path(project) / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text("hello world")
    annotation = await annotation_service.add_annotation(project, path, 0, 5, "note")
    return full_path, annotation["id"]


async def queue_reply(project, annotation_id, task_id="reply"):
    async with UnitOfWork(project) as uow:
        await uow.task_state.set_queued(
            task_id, task_type="annotation_reply",
            owner_type="annotation", owner_id=annotation_id,
        )
    return task_id


@pytest.mark.asyncio
async def test_delete_drains_reply_and_removes_all_annotation_state(project):
    path, annotation_id = await create_annotation(project)
    task_id = await queue_reply(project, annotation_id)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def source(cancel_event):
        started.set()
        await cancel_event.wait()
        assert path.exists()
        stopped.set()
        yield {"type": "cancelled", "data": {}}

    task_runtime.launch(task_id=task_id, project_id=project, source_factory=source)
    await asyncio.wait_for(started.wait(), timeout=3)
    try:
        await file_service.delete_item(project, "paper.md")
        assert stopped.is_set()
        assert not path.exists()
        async with UnitOfWork(project) as uow:
            assert await uow.annotations.get_by_id(annotation_id) is None
            assert await uow.annotations.get_file_state("paper.md") is None
            assert await uow.messages.get_messages_for_annotation_llm(annotation_id) == []
            assert await uow.task_state.get_by_id(task_id) is None
            assert await uow.file_deletions.get_pending() == []
        path.write_text("hello world")
        assert (await annotation_service.get_annotations(project, "paper.md"))["annotations"] == []
    finally:
        task_runtime.cancel(task_id)
        await task_runtime.wait_for_task(task_id)


@pytest.mark.asyncio
async def test_directory_delete_is_recursive_without_matching_sibling_prefixes(project):
    _, first = await create_annotation(project, "notes_%/paper.md")
    _, second = await create_annotation(project, "notes_%/sub/paper.md")
    sibling_path, sibling = await create_annotation(project, "notes_%2/paper.md")
    await file_service.delete_item(project, "notes_%")
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_by_id(first) is None
        assert await uow.annotations.get_by_id(second) is None
        assert await uow.annotations.get_by_id(sibling) is not None
    assert sibling_path.exists()


@pytest.mark.asyncio
async def test_drain_timeout_keeps_barrier_and_allows_retry(project, monkeypatch):
    path, annotation_id = await create_annotation(project)
    await queue_reply(project, annotation_id)
    monkeypatch.setattr(task_runtime, "wait_for_task", AsyncMock(return_value=False))
    with pytest.raises(TaskActiveError):
        await file_service.delete_item(project, "paper.md")
    assert path.exists()
    with pytest.raises(FileSystemError) as error:
        await annotation_service.start_ai_reply_stream(project, "paper.md", annotation_id)
    assert error.value.code == "FILE_DELETING"
    with pytest.raises(FileSystemError):
        await annotation_service.add_annotation(project, "paper.md", 0, 5, "another")
    monkeypatch.setattr(task_runtime, "wait_for_task", AsyncMock(return_value=True))
    await file_service.delete_item(project, "paper.md")
    assert not path.exists()


@pytest.mark.asyncio
async def test_cleanup_failure_after_unlink_recovers_missing_file(project, monkeypatch):
    path, annotation_id = await create_annotation(project)
    original = FileDeletionRepository.stage_finish

    async def fail_finish(*_args):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(FileDeletionRepository, "stage_finish", fail_finish)
    with pytest.raises(RuntimeError, match="injected"):
        await file_service.delete_item(project, "paper.md")
    assert not path.exists()
    async with UnitOfWork(project) as uow:
        assert await uow.file_deletions.get_pending()
        assert await uow.annotations.get_by_id(annotation_id) is not None
    monkeypatch.setattr(FileDeletionRepository, "stage_finish", original)
    await file_deletion_service.recover_deletions(project)
    async with UnitOfWork(project) as uow:
        assert await uow.file_deletions.get_pending() == []
        assert await uow.annotations.get_by_id(annotation_id) is None
    assert not path.exists()


@pytest.mark.asyncio
async def test_stale_recovery_does_not_delete_new_file(project):
    path, _ = await create_annotation(project)
    await file_service.delete_item(project, "paper.md")
    path.write_text("new user content")
    await file_deletion_service.delete_item(project, "paper.md", resume=True)
    assert path.read_text() == "new user content"


@pytest.mark.asyncio
async def test_delete_between_journal_and_write_cannot_resurrect_file(project, monkeypatch):
    path, _ = await create_annotation(project)
    snapshot = await annotation_service.get_annotations(project, "paper.md")
    original = AnnotationRepository.create_transaction

    async def delete_after_journal(repository, **values):
        result = await original(repository, **values)
        await file_service.delete_item(project, "paper.md")
        return result

    monkeypatch.setattr(AnnotationRepository, "create_transaction", delete_after_journal)
    with pytest.raises(AnnotationConflictError):
        await annotation_service.save_document(
            project, "paper.md", "new content", snapshot["fileHash"], snapshot["revision"], [], [],
        )
    await annotation_service.recover_transactions(project)
    assert not path.exists()
    async with UnitOfWork(project) as uow:
        assert await uow.annotations.get_transactions() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [".", ".SiGMA", ".SiGMA/project.db", ".git"])
async def test_file_delete_cannot_remove_project_lifecycle_storage(project, path):
    with pytest.raises(FileSystemError):
        await file_service.delete_item(project, path)
