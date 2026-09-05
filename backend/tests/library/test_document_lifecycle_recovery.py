"""Behavioral tests for the library document cancel/recover lifecycle.

Drives the real service layer against a real per-project SQLite database
(Alembic-migrated); only the RAG vector store is faked (it needs real
models). Each test pins one lifecycle invariant:

- Editing a processing document converges to a reprocess, never deletion.
- The maintenance sweep recovers stuck cancelling documents by marking them
  failed; it never deletes documents.
- A failed indexing status check fails the task instead of faking success.
- rebuild_index cancels active tasks before resetting the RAG collection
  and never strands content-less documents in "cancelling".
"""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.document_status import (
    STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED, STATUS_INDEXING,
    STATUS_PENDING, STATUS_PROCESSING,
)
from app.core.utils import utcnow
from app.database.unit_of_work import UnitOfWork
from app.services import background_task_service as bts
from app.services.index_builder import index_builder
from app.services.library_service import library_service
from app.services.rag_service import rag_service


class _BuilderUow:
    def __init__(self, repo):
        self.library = repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _BuilderRepo:
    def __init__(self, doc, published):
        self.doc = doc
        self.published = published

    async def get_by_id(self, _doc_id):
        return self.doc

    async def publish_index(self, _doc_id, _revision, _generation):
        return self.published


async def _create_doc(project_id, **overrides):
    kwargs = dict(
        title="paper",
        content="",
        source="user upload",
        doc_type="txt",
        processing_status=STATUS_PROCESSING,
    )
    kwargs.update(overrides)
    async with UnitOfWork(project_id) as uow:
        return await uow.library.create(**kwargs)


async def _get_doc(project_id, doc_id):
    async with UnitOfWork(project_id) as uow:
        doc = await uow.library.get_by_id(doc_id)
        return doc.to_dict() if doc else None


# ---------------------------------------------------------------------------
# Edit during processing converges to a reprocess (never deletion)
# ---------------------------------------------------------------------------


async def test_edit_during_processing_reprocesses_and_never_deletes(project, tmp_path):
    """Editing a document that is mid-processing must leave the document in
    the queue for full reprocessing with the new revision — the document,
    its source file, and its DB row all survive."""
    source = tmp_path / "source.txt"
    source.write_text("file body", encoding="utf-8")
    doc = await _create_doc(
        project, file_path=str(source), processing_status=STATUS_PROCESSING,
    )

    updated = await library_service.update_document(project, doc.id, {"title": "paper v2"})

    assert updated["processing_status"] == STATUS_PENDING
    fresh = await _get_doc(project, doc.id)
    assert fresh is not None
    assert fresh["revision"] == doc.revision + 1
    assert fresh["processing_status"] == STATUS_PENDING
    assert source.exists()

    # The reprocess task is claimable and carries the new revision.
    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert task is not None
    assert task.kind == bts.KIND_DOCUMENT_PROCESS
    payload = json.loads(task.payload_json)
    assert payload["doc_id"] == doc.id
    assert payload["doc_revision"] == doc.revision + 1


async def test_edit_during_processing_discards_stale_content_writes(project, tmp_path):
    """A processing task running against an older revision must not clobber
    content written after the user's edit (revision-guarded content write)."""
    source = tmp_path / "source.txt"
    source.write_text("file body", encoding="utf-8")
    doc = await _create_doc(
        project, file_path=str(source), processing_status=STATUS_PROCESSING,
    )

    await library_service.update_document(project, doc.id, {"title": "paper v2"})
    await library_service.update_document_content(
        project, doc.id, "stale extraction", expected_revision=doc.revision,
    )

    fresh = await _get_doc(project, doc.id)
    assert fresh["content"] != "stale extraction"


# ---------------------------------------------------------------------------
# Maintenance sweep recovers cancelling documents without deleting them
# ---------------------------------------------------------------------------


async def test_sweep_resets_lease_expired_cancelling_doc_to_failed(project, tmp_path):
    source = tmp_path / "stuck.txt"
    source.write_text("body", encoding="utf-8")
    doc = await _create_doc(
        project,
        file_path=str(source),
        processing_status=STATUS_CANCELLING,
    )
    async with UnitOfWork(project) as uow:
        await uow.library.update_processing_status(
            doc.id,
            status=STATUS_CANCELLING,
            started_at=utcnow() - timedelta(seconds=7200),
        )

    recovered = await bts.background_task_service._scan_library_project(project)

    fresh = await _get_doc(project, doc.id)
    assert fresh is not None, "the sweep must never delete a document"
    assert fresh["processing_status"] == STATUS_FAILED
    assert source.exists()
    assert recovered >= 1


async def test_sweep_resets_cancelling_doc_without_started_at(project):
    """A cancelling row without a start timestamp can never be lease-expired;
    the sweep must still converge it to failed."""
    doc = await _create_doc(project, processing_status=STATUS_CANCELLING)
    async with UnitOfWork(project) as uow:
        fresh = await uow.library.get_by_id(doc.id)
        fresh.processing_started_at = None
        await uow.commit()

    await bts.background_task_service._scan_library_project(project)

    got = await _get_doc(project, doc.id)
    assert got is not None
    assert got["processing_status"] == STATUS_FAILED


async def test_sweep_leaves_recent_cancelling_doc_in_place(project):
    """A cancelling document inside its lease is still converging; the
    sweep only re-asserts task cancellation."""
    doc = await _create_doc(project, processing_status=STATUS_CANCELLING)
    async with UnitOfWork(project) as uow:
        await uow.library.update_processing_status(
            doc.id,
            status=STATUS_CANCELLING,
            started_at=utcnow(),
        )

    await bts.background_task_service._scan_library_project(project)

    fresh = await _get_doc(project, doc.id)
    assert fresh is not None
    assert fresh["processing_status"] == STATUS_CANCELLING


async def test_sweep_unblocks_task_row_stuck_in_cancelling_past_its_lease(project):
    """A cancelled attempt never receives a hidden fresh retry budget."""
    doc = await _create_doc(
        project, content="indexable body", processing_status=STATUS_INDEXING,
    )
    await bts.background_task_service.enqueue_rag_index(project, doc.id, wake=False)
    async with UnitOfWork(project) as uow:
        claimed = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="runner-1", lease_seconds=60,
        )
    assert claimed is not None

    # The user cancels: RUNNING -> CANCELLING with the lease token kept.
    async with UnitOfWork(project) as uow:
        await uow.background_tasks.cancel_by_dedupe_prefix(
            f"{bts.KIND_RAG_INDEX}:{project}:{doc.id}",
        )
    # The runner dies mid wind-down: the lease expires with no heartbeat.
    async with UnitOfWork(project) as uow:
        row = await uow.background_tasks.get_by_id(claimed.id)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
        await uow.commit()

    await bts.background_task_service._scan_library_project(project)

    # Recovery terminalizes the stuck row and makes the document visibly
    # failed. A user-triggered reprocess is required for another paid attempt.
    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="runner-2", lease_seconds=60,
        )
        fresh = await uow.library.get_by_id(doc.id)
    assert task is None
    assert fresh.processing_status == "failed"


# ---------------------------------------------------------------------------
# Indexing status-check failure fails the task (never fake success)
# ---------------------------------------------------------------------------


async def test_status_check_failure_fails_index_task(project, monkeypatch):
    """When the cross-thread status check raises, the indexing task must
    fail (retryable) instead of marking the document completed with the
    old chunks purged."""
    doc = await _create_doc(
        project, content="indexable body", processing_status=STATUS_INDEXING,
    )

    async def broken_check(_project_id, _doc_id, _expected_revision=None):
        raise RuntimeError("status check unavailable")

    monkeypatch.setattr(index_builder, "_doc_gone_or_stale", broken_check)

    async def fake_index_document(_project_id, _doc_id, _content, title="",
                                  description="", should_continue=None, doc_revision=None,
                                  index_generation=None):
        # Mimic the real pipeline: the check runs in a worker thread.
        await asyncio.get_running_loop().run_in_executor(None, should_continue)

    monkeypatch.setattr(rag_service, "index_document", fake_index_document)

    with pytest.raises(RuntimeError, match="status check unavailable"):
        await index_builder.process_one(project, doc.id, expected_revision=doc.revision)

    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] != STATUS_COMPLETED


# ---------------------------------------------------------------------------
# Lease loss stops indexing without a stale finalize
# ---------------------------------------------------------------------------


async def test_lease_loss_mid_indexing_stops_writes_and_leaves_no_stale_finalize(
    project, monkeypatch,
):
    """A lease-loss signal stops writes and leaves state to the new claimant."""
    from app.services.library_task_protocol import RunningTaskContext

    doc = await _create_doc(
        project, content="indexable body", processing_status=STATUS_INDEXING,
    )
    await bts.background_task_service.enqueue_rag_index(project, doc.id, wake=False)
    async with UnitOfWork(project) as uow:
        claimed = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert claimed is not None

    ctx = RunningTaskContext(
        project_id=project, task_id=claimed.id,
        owner=claimed.lease_owner, lease_seconds=60,
    )

    writes = []
    stored = []

    async def fake_index_document(_project_id, _doc_id, _content, title="",
                                  description="", should_continue=None, doc_revision=None,
                                  index_generation=None):
        # The queue runner's heartbeat task signals lease loss through the
        # shared cancel event; indexing checks it before every batch.
        loop = asyncio.get_running_loop()
        for _ in range(10):
            if not await loop.run_in_executor(None, should_continue):
                break
            writes.append("chunk")
            ctx.cancel_event.set()
        else:
            stored.append("store")

    monkeypatch.setattr(rag_service, "index_document", fake_index_document)

    completed = await index_builder.process_one(
        project, doc.id, expected_revision=doc.revision, task_context=ctx,
    )

    assert completed is True
    assert ctx.cancel_event.is_set()
    # Indexing stopped after the runner signalled the lease loss; the final
    # store never ran.
    assert writes == ["chunk"]
    assert stored == []

    # The stale run owns nothing anymore: the document keeps its in-flight
    # status (no failed, no completed) and the task row keeps the claim the
    # new owner wrote.
    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] == STATUS_INDEXING
    async with UnitOfWork(project) as uow:
        row = await uow.background_tasks.get_by_id(claimed.id)
    assert row.status == "running"
    assert row.lease_owner == claimed.lease_owner


async def test_rebuild_index_cancels_active_docs_before_collection_reset(project, monkeypatch):
    events = []
    doc_with_content = await _create_doc(
        project, content="body", processing_status=STATUS_PROCESSING,
    )
    doc_without_content = await _create_doc(
        project, content="", processing_status=STATUS_PROCESSING,
    )

    real_cancel = library_service._cancel_processing

    async def recording_cancel(project_id, doc_id):
        events.append(f"cancel:{doc_id}")
        await real_cancel(project_id, doc_id)

    async def fake_reset(_project_id):
        events.append("reset_collection")

    async def fake_enqueue_rag_index(_pid, doc_id, **_kwargs):
        return f"rag-{doc_id}"

    async def fake_enqueue_document_process(_pid, doc_id, **_kwargs):
        return f"proc-{doc_id}"

    monkeypatch.setattr(library_service, "_cancel_processing", recording_cancel)
    monkeypatch.setattr(rag_service, "reset_project_index", fake_reset)
    monkeypatch.setattr(
        bts.background_task_service, "enqueue_rag_index", fake_enqueue_rag_index,
    )
    monkeypatch.setattr(
        bts.background_task_service, "enqueue_document_process", fake_enqueue_document_process,
    )

    result = await library_service.rebuild_index(project)

    # Every cancel happens before the collection reset; nothing survives it.
    assert set(events[:-1]) == {f"cancel:{doc_with_content.id}", f"cancel:{doc_without_content.id}"}
    assert events[-1] == "reset_collection"
    assert result["total"] == 2

    # Content documents re-index; content-less ones reprocess instead of
    # being stranded in "cancelling".
    fresh_with = await _get_doc(project, doc_with_content.id)
    fresh_without = await _get_doc(project, doc_without_content.id)
    assert fresh_with["processing_status"] == STATUS_INDEXING
    assert fresh_without["processing_status"] == STATUS_PENDING


# ---------------------------------------------------------------------------
# The rebuild's post-reset re-enqueue comes from a fresh read
# ---------------------------------------------------------------------------


async def test_rebuild_index_picks_up_doc_completed_after_snapshot(project, monkeypatch):
    """A document that gains content (completes processing) between the
    rebuild's snapshot and the collection reset must be re-enqueued by the
    fresh post-reset read instead of being left with deleted chunks."""
    doc = await _create_doc(project, content="", processing_status=STATUS_COMPLETED)

    async def fake_reset_and_complete(_project_id):
        async with UnitOfWork(project) as uow:
            await uow.library.update_content(doc.id, "content landed mid-rebuild")

    enqueued = []

    async def fake_enqueue_rag_index(_pid, doc_id, **_kwargs):
        enqueued.append(doc_id)
        return f"rag-{doc_id}"

    monkeypatch.setattr(rag_service, "reset_project_index", fake_reset_and_complete)
    monkeypatch.setattr(
        bts.background_task_service, "enqueue_rag_index", fake_enqueue_rag_index,
    )

    result = await library_service.rebuild_index(project)

    assert result["total"] == 1
    assert enqueued == [doc.id]
    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] == STATUS_INDEXING


# ---------------------------------------------------------------------------
# Failure states stay visible and recoverable
# ---------------------------------------------------------------------------


async def test_sweep_does_not_touch_failed_or_completed_docs(project):
    """The sweep's recovery list is status-scoped: terminal docs stay put."""
    doc = await _create_doc(project, processing_status=STATUS_FAILED)

    await bts.background_task_service._scan_library_project(project)

    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] == STATUS_FAILED


# ---------------------------------------------------------------------------
# rebuild_index survives a mid-rebuild enqueue failure via the sweep
# ---------------------------------------------------------------------------


async def test_rebuild_index_enqueue_failure_stays_sweep_recoverable(project, monkeypatch):
    """Documents batch-reset before the destructive collection reset stay
    visible as indexing/pending even when enqueueing crashes mid-rebuild;
    the maintenance sweep then re-enqueues them on its own."""
    doc_a = await _create_doc(project, content="body a", processing_status=STATUS_COMPLETED)
    doc_b = await _create_doc(project, content="body b", processing_status=STATUS_COMPLETED)

    real_enqueue_rag_index = bts.background_task_service.enqueue_rag_index
    rebuild_phase = True

    async def flaky_enqueue(_pid, _doc_id, **_kwargs):
        if rebuild_phase:
            raise RuntimeError("queue down")
        return await real_enqueue_rag_index(_pid, _doc_id, **_kwargs)

    monkeypatch.setattr(bts.background_task_service, "enqueue_rag_index", flaky_enqueue)

    result = await library_service.rebuild_index(project)
    assert result["total"] == 2

    fresh_a = await _get_doc(project, doc_a.id)
    fresh_b = await _get_doc(project, doc_b.id)
    assert fresh_a["processing_status"] == STATUS_INDEXING
    assert fresh_b["processing_status"] == STATUS_INDEXING

    # With enqueueing healthy again, the sweep re-enqueues both documents.
    rebuild_phase = False
    recovered = await bts.background_task_service._scan_library_project(project)
    assert recovered >= 2

    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert task is not None
    assert task.kind == bts.KIND_RAG_INDEX


# ---------------------------------------------------------------------------
# The sweep never cancels tasks for a document that was re-enqueued after
# its snapshot was taken
# ---------------------------------------------------------------------------


async def test_sweep_skips_cancel_for_doc_requeued_after_snapshot(project, monkeypatch):
    """A cancelling document whose state flips back to pending between the
    sweep snapshot and the fresh read must keep its (fresh) tasks."""
    from app.database.repos.library_repo import LibraryRepository

    doc = await _create_doc(project, processing_status=STATUS_CANCELLING)

    real_get_by_id = LibraryRepository.get_by_id
    flipped = False

    async def flip_on_first_read(self, doc_id):
        nonlocal flipped
        if not flipped and doc_id == doc.id:
            flipped = True
            async with UnitOfWork(project) as uow:
                await uow.library.update_processing_status(
                    doc.id, status=STATUS_PENDING,
                )
        return await real_get_by_id(self, doc_id)

    monkeypatch.setattr(LibraryRepository, "get_by_id", flip_on_first_read)

    cancel_calls = []

    async def recording_cancel(project_id, doc_id):
        cancel_calls.append(doc_id)
        return 0

    monkeypatch.setattr(
        bts.background_task_service, "cancel_document_tasks", recording_cancel,
    )

    await bts.background_task_service._scan_library_project(project)

    assert cancel_calls == []
    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] == STATUS_PENDING


# ---------------------------------------------------------------------------
# Status transitions are revision-guarded against stale tasks
# ---------------------------------------------------------------------------


async def _edit_document(project_id, doc_id):
    """Land a user edit's net effect: revision bump + reset for reprocess."""
    async with UnitOfWork(project_id) as uow:
        await uow.library.update_fields(doc_id, title="paper v2", bump_revision=True)
        current = await uow.library.get_by_id(doc_id)
        await uow.library.update_processing_status(
            doc_id, STATUS_PENDING, expected_revision=current.revision,
        )


async def test_index_completion_write_is_revision_guarded(project, monkeypatch):
    """An edit landing between the post-check read and the completion write
    must win: the guarded write is skipped so the edit's own re-enqueued task
    owns the document outcome."""
    doc = await _create_doc(
        project, content="indexable body", processing_status=STATUS_INDEXING,
    )
    old_revision = doc.revision
    await _edit_document(project, doc.id)

    completed = await index_builder.process_one(
        project, doc.id, expected_revision=old_revision,
    )

    assert completed is True
    fresh = await _get_doc(project, doc.id)
    assert fresh["revision"] == doc.revision + 1
    assert fresh["processing_status"] == STATUS_PENDING


async def test_empty_content_completion_write_is_revision_guarded(project, monkeypatch):
    """Same guard for the empty-content shortcut: an edit racing the
    completion write is not overwritten with 'completed'."""
    doc = await _create_doc(project, content="", processing_status=STATUS_INDEXING)
    old_revision = doc.revision
    await _edit_document(project, doc.id)

    completed = await index_builder.process_one(
        project, doc.id, expected_revision=old_revision,
    )

    assert completed is True
    fresh = await _get_doc(project, doc.id)
    assert fresh["revision"] == doc.revision + 1
    assert fresh["processing_status"] == STATUS_PENDING


async def test_index_failure_marking_is_revision_guarded(project):
    """A stale index task's failure write must not clobber a document that
    was edited (and re-enqueued) while indexing ran."""
    doc = await _create_doc(
        project, content="indexable body", processing_status=STATUS_INDEXING,
    )
    await _edit_document(project, doc.id)

    await index_builder._mark_doc_failed(
        project, doc.id, "boom", expected_revision=doc.revision,
    )

    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] == STATUS_PENDING


async def test_mark_status_transitions_reject_stale_revision(project):
    doc = await _create_doc(project, content="body")

    assert await library_service.mark_document_processing(
        project, doc.id, expected_revision=doc.revision,
    ) is True

    # A user edit bumps the revision; a stale task holding the old revision
    # must not overwrite the newer state.
    await library_service.update_document(project, doc.id, {"title": "paper v2"})

    assert await library_service.mark_document_indexing(
        project, doc.id, expected_revision=doc.revision,
    ) is False

    fresh = await _get_doc(project, doc.id)
    assert fresh["processing_status"] == STATUS_PENDING


async def test_mark_status_transitions_reject_missing_document(project):
    assert await library_service.mark_document_processing(
        project, "missing-doc", expected_revision=None,
    ) is False
    assert await library_service.mark_document_indexing(
        project, "missing-doc", expected_revision=None,
    ) is False


# ---------------------------------------------------------------------------
# Editing a failed conversion (no content) reprocesses instead of re-indexing
# ---------------------------------------------------------------------------


async def test_edit_failed_contentless_doc_reprocesses_instead_of_reindexing(project):
    """A failed conversion has no content to index; renaming it must send
    the document back to full processing, not complete an empty index."""
    doc = await _create_doc(project, content="", processing_status=STATUS_FAILED)

    updated = await library_service.update_document(project, doc.id, {"title": "renamed"})

    assert updated["processing_status"] == STATUS_PENDING

    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert task is not None
    assert task.kind == bts.KIND_DOCUMENT_PROCESS


async def test_edit_failed_doc_with_content_still_reindexes(project):
    doc = await _create_doc(project, content="indexable body", processing_status=STATUS_FAILED)

    updated = await library_service.update_document(project, doc.id, {"title": "renamed"})

    assert updated["processing_status"] == STATUS_INDEXING


# ---------------------------------------------------------------------------
# rebuild_index waits for in-flight handlers before the destructive reset
# ---------------------------------------------------------------------------


async def test_rebuild_index_waits_for_inflight_task_before_reset(project, monkeypatch):
    """The collection reset is delayed until the in-flight handler finalized
    its task row: cancelling is cooperative, and a handler past its last
    checkpoint may still write chunks into the collection being reset."""
    import app.services.library_service as library_module

    monkeypatch.setattr(library_module, "_REBUILD_DRAIN_POLL_SECONDS", 0.01)

    doc = await _create_doc(project, content="body", processing_status=STATUS_INDEXING)
    from app.services.background_task_service import background_task_service

    await background_task_service.enqueue_rag_index(project, doc.id, wake=False)
    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert task is not None

    events = []

    async def fake_reset(_project_id):
        events.append("reset_collection")

    monkeypatch.setattr(rag_service, "reset_project_index", fake_reset)

    rebuild_task = asyncio.create_task(library_service.rebuild_index(project))

    # The cancel flipped the task to CANCELLING; the drain wait must hold
    # the reset back until the handler finalizes the row.
    async def wait_until_cancelling():
        while True:
            async with UnitOfWork(project) as uow:
                row = await uow.background_tasks.get_by_id(task.id)
            if row.status == "cancelling":
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_until_cancelling(), timeout=5)
    await asyncio.sleep(0.05)
    assert "reset_collection" not in events

    # The handler winds down: finalize the row as its owner would.
    async with UnitOfWork(project) as uow:
        await uow.background_tasks.mark_cancelled(task.id, task.lease_owner)
    events.append("handler_finalized")

    await asyncio.wait_for(rebuild_task, timeout=5)

    assert "handler_finalized" in events
    assert events.index("handler_finalized") < events.index("reset_collection")


async def test_concurrent_rebuild_is_rejected_before_duplicate_work(project, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def claimed(project_id):
        calls.append(project_id)
        entered.set()
        await release.wait()
        return {"success": True, "status": "queued"}

    monkeypatch.setattr(library_service, "_rebuild_index_claimed", claimed)

    first = asyncio.create_task(library_service.rebuild_index(project))
    await entered.wait()
    second = await library_service.rebuild_index(project)
    release.set()

    assert await first == {"success": True, "status": "queued"}
    assert second["success"] is False
    assert second["status"] == "already_running"
    assert calls == [project]


# ---------------------------------------------------------------------------
# create_document starts from the status the pipeline actually runs
# ---------------------------------------------------------------------------


async def test_create_document_with_content_starts_indexing(project):
    """A content document is born 'indexing' (the status the sweep
    re-enqueues) so a crash after the create cannot strand a 'completed'
    document whose chunks were never written."""
    result = await library_service.create_document(
        project, {"title": "doc", "content": "indexable body"},
    )

    assert result["processing_status"] == STATUS_INDEXING

    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert task is not None
    assert task.kind == bts.KIND_RAG_INDEX
    assert json.loads(task.payload_json)["doc_id"] == result["id"]


async def test_create_document_without_content_completes_without_task(project):
    result = await library_service.create_document(
        project, {"title": "empty doc", "content": ""},
    )

    assert result["processing_status"] == STATUS_COMPLETED

    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )
    assert task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_index_publish_cleanup_obeys_exact_prepare_publish_protocol(
    monkeypatch, published,
):
    doc = SimpleNamespace(
        id="doc-1",
        content="body",
        title="title",
        description="",
        revision=5,
        index_generation=9,
        indexed_revision=4,
        indexed_generation=8,
        processing_status=STATUS_INDEXING,
    )
    repo = _BuilderRepo(doc, published)
    monkeypatch.setattr(
        "app.services.index_builder.UnitOfWork",
        lambda _project_id: _BuilderUow(repo),
    )
    monkeypatch.setattr(
        rag_service, "index_document", lambda *_args, **_kwargs: _async_true(),
    )
    cleanups = []

    async def fake_cleanup(project_id, doc_id, revision, generation):
        cleanups.append((project_id, doc_id, revision, generation))

    monkeypatch.setattr(rag_service, "cleanup_generation", fake_cleanup)

    result = await index_builder.process_one("project", "doc-1")

    assert result is True
    if published:
        assert ("project", "doc-1", 5, 9) not in cleanups
        assert ("project", "doc-1", 4, 8) in cleanups
    else:
        assert ("project", "doc-1", 5, 9) in cleanups
        assert ("project", "doc-1", 4, 8) not in cleanups


async def _async_true():
    return True
