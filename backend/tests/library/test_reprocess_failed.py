"""Reprocessing failed documents: only failed docs are accepted, and the
reset keeps the previous failure reason visible in the processing log."""

import json

import pytest

from app.core.document_status import STATUS_FAILED, STATUS_PENDING, STATUS_PROCESSING
from app.core.exceptions import DocumentNotFoundError, ServiceException
from app.database.unit_of_work import UnitOfWork
from app.services import background_task_service as bts
from app.services.document_processing_service import document_processing_service


async def _create_doc(project_id, **overrides):
    kwargs = dict(
        title="paper",
        content="",
        source="user upload",
        doc_type="txt",
        processing_status=STATUS_FAILED,
    )
    kwargs.update(overrides)
    async with UnitOfWork(project_id) as uow:
        return await uow.library.create(**kwargs)


async def _get_doc(project_id, doc_id):
    async with UnitOfWork(project_id) as uow:
        doc = await uow.library.get_by_id(doc_id)
        return doc.to_dict() if doc else None


async def _get_processing_log(project_id, doc_id):
    async with UnitOfWork(project_id) as uow:
        doc = await uow.library.get_by_id(doc_id)
        return doc.processing_log if doc else None


async def test_reprocess_rejects_document_that_is_not_failed(project, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("body", encoding="utf-8")
    doc = await _create_doc(
        project, file_path=str(source), processing_status=STATUS_PROCESSING,
    )

    with pytest.raises(ServiceException) as exc_info:
        await document_processing_service.reprocess_failed(project, doc.id)

    assert exc_info.value.status_code == 409
    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] == STATUS_PROCESSING


async def test_reprocess_failed_document_preserves_failure_reason(project, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("body", encoding="utf-8")
    doc = await _create_doc(project, file_path=str(source))
    async with UnitOfWork(project) as uow:
        await uow.library.mark_failed(doc.id, "Docling conversion exploded")

    result = await document_processing_service.reprocess_failed(project, doc.id)

    assert result["success"] is True
    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] == STATUS_PENDING
    assert fresh["revision"] == doc.revision + 1
    assert fresh["indexed_revision"] is None
    log = await _get_processing_log(project, doc.id)
    assert "Docling conversion exploded" in log
    assert "Reprocessing started" in log

    # The reprocess task is claimable and carries the current revision.
    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert task is not None
    assert task.kind == bts.KIND_DOCUMENT_PROCESS
    assert json.loads(task.payload_json)["doc_id"] == doc.id
    assert json.loads(task.payload_json)["doc_revision"] == fresh["revision"]


async def test_stale_failure_cannot_overwrite_reprocessed_generation(project, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("body", encoding="utf-8")
    doc = await _create_doc(project, file_path=str(source))
    async with UnitOfWork(project) as uow:
        old_revision = doc.revision
        await uow.library.mark_failed(doc.id, "old failure", expected_revision=old_revision)
        new_revision = await uow.library.reset_processing(doc.id)
        applied = await uow.library.mark_failed(
            doc.id, "stale failure", expected_revision=old_revision,
        )

    assert new_revision == old_revision + 1
    assert applied is False
    fresh = await _get_doc(project, doc.id)
    assert fresh["revision"] == new_revision
    assert fresh["processing_status"] == STATUS_PENDING


async def test_reprocess_missing_document_raises_not_found(project):
    with pytest.raises(DocumentNotFoundError):
        await document_processing_service.reprocess_failed(project, "missing-doc")
