"""Phased RAG orphan-chunk cleanup.

Pins the phase ordering that keeps the valid-document read fresh (chunk
scan first, database read only after it returns), the empty-library reset
branch, and the self-heal backstop that re-enqueues a document whose
chunks were removed while the document still exists.
"""

import threading
from types import SimpleNamespace

from app.core.config import settings
from app.core.document_status import STATUS_COMPLETED, STATUS_INDEXING
from app.database.unit_of_work import UnitOfWork
from app.services import background_task_service as bts
from app.services.rag_service import rag_service


async def _create_doc(project_id, **overrides):
    kwargs = dict(
        title="paper",
        content="indexable body",
        source="user upload",
        doc_type="txt",
        processing_status=STATUS_COMPLETED,
    )
    kwargs.update(overrides)
    async with UnitOfWork(project_id) as uow:
        return await uow.library.create(**kwargs)


async def _claim_next(project_id):
    async with UnitOfWork(project_id) as uow:
        return await uow.background_tasks.claim_next(
            queue=bts.QUEUE_LIBRARY, owner="test", lease_seconds=60,
        )


# ---------------------------------------------------------------------------
# Phase ordering: the valid-id read follows the chunk scan
# ---------------------------------------------------------------------------


async def test_valid_ids_are_read_after_the_chunk_scan(monkeypatch):
    """The chunk whose document was deleted is orphaned, but the fresh read
    taken only after the scan sees the document that was created in
    between — its chunks survive."""
    order = []

    async def fake_chunk_scan(_project_id):
        order.append("scan")
        return {"d1", "d2"}

    async def provider():
        order.append("valid_read")
        return {"d2"}  # d1 is gone; d2 landed after the scan

    deletions = []

    def fake_delete(_project_id, orphans, _valid):
        order.append("delete")
        deletions.append(set(orphans))

    monkeypatch.setattr(rag_service, "collection_doc_ids", fake_chunk_scan)
    monkeypatch.setattr(rag_service, "_sync_remove_orphan_chunks", fake_delete)

    removed = await rag_service.cleanup_orphans("p1", provider)

    assert order == ["scan", "valid_read", "delete"]
    assert removed == {"d1"}
    assert deletions == [{"d1"}]


async def test_no_orphans_skips_the_delete_phase(monkeypatch):
    scans = []

    async def fake_chunk_scan(_project_id):
        scans.append(1)
        return {"d1"}

    async def provider():
        return {"d1"}

    monkeypatch.setattr(rag_service, "collection_doc_ids", fake_chunk_scan)

    removed = await rag_service.cleanup_orphans("p1", provider)

    assert removed == set()
    assert len(scans) == 1


async def test_empty_library_resets_the_collection(monkeypatch):
    async def fake_chunk_scan(_project_id):
        return {"d1"}

    resets = []

    async def fake_reset(_project_id):
        resets.append(_project_id)

    async def provider():
        return set()

    monkeypatch.setattr(rag_service, "collection_doc_ids", fake_chunk_scan)
    monkeypatch.setattr(rag_service, "reset_project_index", fake_reset)

    removed = await rag_service.cleanup_orphans("p1", provider)

    assert resets == ["p1"]
    assert removed == {"d1"}


# ---------------------------------------------------------------------------
# Full phased flow against a real Chroma store (models faked)
# ---------------------------------------------------------------------------


class _FakeProjectState:
    """Just enough of RAGService._ProjectState for the delete phase."""

    def __init__(self, collection):
        self.vector_store = SimpleNamespace(_collection=collection)
        self.all_nodes = []
        self.bm25_index = None
        self.all_nodes_lock = threading.Lock()


def _seed_chroma_collection(project_id, doc_ids, published=None):
    import chromadb

    chroma_dir = settings.get_sigma_path(project_id) / "chroma"
    client = chromadb.PersistentClient(path=str(chroma_dir))
    collection = client.get_or_create_collection(name="library")
    ids, metadatas, embeddings = [], [], []
    for index, doc_id in enumerate(doc_ids):
        for chunk in range(2):
            ids.append(f"{doc_id}_chunk{chunk}")
            metadata = {"doc_id": doc_id}
            if published and doc_id in published:
                revision, generation = published[doc_id]
                metadata.update(doc_revision=revision, index_generation=generation)
            metadatas.append(metadata)
            embeddings.append([float(index + 1), 0.0])
    collection.add(ids=ids, metadatas=metadatas, embeddings=embeddings)
    rag_service._drop_cached_chroma_system(chroma_dir)
    return collection


async def test_phase_deletion_keeps_document_created_between_phases(project, monkeypatch):
    """End to end: chunks exist for an existing doc and for a doc that was
    deleted before the run; only the deleted doc's chunks go."""
    doc = await _create_doc(project)
    async with UnitOfWork(project) as uow:
        current = await uow.library.get_by_id(doc.id)
        current.indexed_revision = current.revision
        current.indexed_generation = current.index_generation
        await uow.session.commit()
    collection = _seed_chroma_collection(
        project, [doc.id, "deleted-doc"],
        published={doc.id: (doc.revision, doc.index_generation)},
    )

    monkeypatch.setattr(rag_service, "_ensure_init", lambda: None)
    monkeypatch.setattr(
        rag_service, "_get_project", lambda _pid: _FakeProjectState(collection),
    )

    await bts.background_task_service._cleanup_project_orphan_chunks(project)

    remaining = collection.get(include=["metadatas"])
    remaining_docs = {meta["doc_id"] for meta in remaining["metadatas"]}
    assert remaining_docs == {doc.id}


# ---------------------------------------------------------------------------
# Cleanup never starts a paid rebuild without user action
# ---------------------------------------------------------------------------


async def test_cleanup_does_not_reenqueue_completed_document_that_lost_chunks(
    project, monkeypatch,
):
    doc = await _create_doc(project)

    async def fake_cleanup(_project_id, _provider):
        return {doc.id}

    monkeypatch.setattr(rag_service, "cleanup_orphans", fake_cleanup)
    async def fake_stale_cleanup(*_args):
        return set()

    monkeypatch.setattr(rag_service, "cleanup_stale_generations", fake_stale_cleanup)

    await bts.background_task_service._cleanup_project_orphan_chunks(project)

    assert await _claim_next(project) is None


async def test_cleanup_reenqueues_active_indexing_document_that_lost_chunks(
    project, monkeypatch,
):
    doc = await _create_doc(project, processing_status=STATUS_INDEXING)

    async def fake_cleanup(_project_id, _provider):
        return {doc.id}

    async def fake_stale_cleanup(*_args):
        return set()

    monkeypatch.setattr(rag_service, "cleanup_orphans", fake_cleanup)
    monkeypatch.setattr(rag_service, "cleanup_stale_generations", fake_stale_cleanup)

    await bts.background_task_service._cleanup_project_orphan_chunks(project)

    task = await _claim_next(project)
    assert task is not None
    assert task.kind == bts.KIND_RAG_INDEX


async def test_cleanup_does_not_reenqueue_removed_missing_document(
    project, monkeypatch,
):
    async def fake_cleanup(_project_id, _provider):
        return {"gone-doc"}

    monkeypatch.setattr(rag_service, "cleanup_orphans", fake_cleanup)
    async def fake_stale_cleanup(*_args):
        return set()

    monkeypatch.setattr(rag_service, "cleanup_stale_generations", fake_stale_cleanup)

    await bts.background_task_service._cleanup_project_orphan_chunks(project)

    assert await _claim_next(project) is None


async def test_legacy_chunk_without_revision_requires_explicit_rebuild(
    project, monkeypatch,
):
    doc = await _create_doc(project)
    async with UnitOfWork(project) as uow:
        current = await uow.library.get_by_id(doc.id)
        current.indexed_revision = current.revision
        current.indexed_generation = 0
        await uow.session.commit()

    class LegacyCollection:
        def __init__(self):
            self.records = {"legacy": {"doc_id": doc.id}}

        def get(self, include=None):
            return {"ids": list(self.records), "metadatas": list(self.records.values())}

        def delete(self, ids):
            for record_id in ids:
                self.records.pop(record_id, None)

    collection = LegacyCollection()
    monkeypatch.setattr(
        rag_service, "_get_project",
        lambda _pid: _FakeProjectState(collection),
    )

    async def fake_cleanup(_project_id, _provider):
        return set()

    monkeypatch.setattr(rag_service, "cleanup_orphans", fake_cleanup)

    await bts.background_task_service._cleanup_project_orphan_chunks(project)

    assert await _claim_next(project) is None
