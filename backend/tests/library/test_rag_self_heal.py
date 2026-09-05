"""RAG index store resilience.

Covers the maintenance sweep's self-heal for an emptied collection and the
clear, actionable error (plus rebuild recovery) for a damaged Chroma store.
"""

from types import SimpleNamespace

import pytest

from app.core.document_status import (
    STATUS_CANCELLING,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from app.core.exceptions import ServiceException
from app.database.unit_of_work import UnitOfWork
from app.services import background_task_service as bts
from app.services.library_service import library_service
from app.services.rag_service import rag_service


async def _create_doc(project_id, **overrides):
    kwargs = dict(
        title="paper",
        content="",
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
# (a) Empty collection with completed content docs → report only
# ---------------------------------------------------------------------------


async def test_sweep_reports_empty_collection_without_reenqueueing(project, monkeypatch):
    doc = await _create_doc(project, content="indexable body")

    async def empty_collection(_project_id):
        return 0

    monkeypatch.setattr(rag_service, "collection_count", empty_collection)

    recovered = await bts.background_task_service._scan_library_project(project)

    assert recovered == 0
    assert await _claim_next(project) is None

    summary = await library_service.get_status_summary(project)
    assert summary["rebuild_needed"] is True


async def test_sweep_leaves_projects_with_indexed_chunks_alone(project, monkeypatch):
    await _create_doc(project, content="indexable body")

    async def healthy_collection(_project_id):
        return 5

    monkeypatch.setattr(rag_service, "collection_count", healthy_collection)

    recovered = await bts.background_task_service._scan_library_project(project)

    assert recovered == 0
    assert await _claim_next(project) is None


@pytest.mark.parametrize("status", [STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLING])
async def test_non_active_documents_do_not_accept_implicit_enqueue(project, status):
    doc = await _create_doc(project, content="indexable body", processing_status=status)

    task_id = await bts.background_task_service.enqueue_rag_index(
        project, doc.id, wake=False,
    )

    assert task_id is None
    assert await _claim_next(project) is None


async def test_terminal_task_does_not_receive_a_fresh_retry_budget(project):
    doc = await _create_doc(
        project,
        content="indexable body",
        processing_status="indexing",
    )
    task_id = await bts.background_task_service.enqueue_rag_index(
        project, doc.id, wake=False,
    )
    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.get_by_id(task_id)
        task.status = "failed"
        task.error = "retry budget exhausted"
        await uow.commit()

    await bts.background_task_service._scan_library_project(project)

    async with UnitOfWork(project) as uow:
        task = await uow.background_tasks.get_by_id(task_id)
        fresh = await uow.library.get_by_id(doc.id)
    assert task.status == "failed"
    assert fresh.processing_status == "failed"
    assert await _claim_next(project) is None


async def test_sweep_survives_chunk_count_failure(project, monkeypatch):
    await _create_doc(project, content="indexable body")

    async def broken_count(_project_id):
        raise RuntimeError("chroma unavailable")

    monkeypatch.setattr(rag_service, "collection_count", broken_count)

    recovered = await bts.background_task_service._scan_library_project(project)

    assert recovered == 0


# ---------------------------------------------------------------------------
# (b) Damaged store: clear error, and rebuild removes it for a fresh start
# ---------------------------------------------------------------------------


def test_open_chroma_store_maps_corruption_to_clear_error(tmp_path, monkeypatch):
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    (chroma_dir / "chroma.sqlite3").write_text("not a database")
    # Drop chroma's per-path system cache so a previous test's client cannot
    # leak into this one.
    rag_service._drop_cached_chroma_system(chroma_dir)

    with pytest.raises(ServiceException) as exc_info:
        rag_service._open_chroma_store("p1", chroma_dir)

    assert exc_info.value.code == "RAG_INDEX_STORE_DAMAGED"
    assert "rebuild" in str(exc_info.value).lower()


def test_reset_removes_damaged_store_so_rebuild_recreates_it(tmp_path, monkeypatch):
    import chromadb

    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    (chroma_dir / "chroma.sqlite3").write_text("not a database")
    monkeypatch.setattr(rag_service, "_ensure_init", lambda: None)
    monkeypatch.setattr(rag_service, "_embedding_identity", None, raising=False)
    monkeypatch.setattr(
        "app.services.rag_service.settings",
        type("S", (), {"get_sigma_path": staticmethod(
            lambda _pid: tmp_path)}),
    )
    rag_service._drop_cached_chroma_system(chroma_dir)

    rag_service._sync_reset_project("p1")

    assert not (chroma_dir / "chroma.sqlite3").exists()
    # The recreated store is usable again in this same process.
    client = chromadb.PersistentClient(path=str(chroma_dir))
    assert client.get_or_create_collection(name="library").count() == 0


def test_collection_count_zero_for_missing_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.services.rag_service.settings",
        type("S", (), {"get_sigma_path": staticmethod(
            lambda _pid: tmp_path)}),
    )

    assert rag_service._sync_collection_count("p1") == 0


class _StubChromaClient:
    def __init__(self, error: Exception | None = None, count: int = 0):
        self._error = error
        self._count = count

    def get_collection(self, name):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(count=lambda: self._count)


def _stub_chroma_env(tmp_path, monkeypatch, client):
    (tmp_path / "chroma").mkdir()
    monkeypatch.setattr(
        "app.services.rag_service.settings",
        type("S", (), {"get_sigma_path": staticmethod(
            lambda _pid: tmp_path)}),
    )
    monkeypatch.setattr(
        rag_service, "_open_chroma_store", lambda _pid, _dir: client,
    )


def test_collection_count_zero_for_missing_collection(tmp_path, monkeypatch):
    from chromadb.errors import NotFoundError

    _stub_chroma_env(
        tmp_path, monkeypatch,
        _StubChromaClient(error=NotFoundError("Collection library does not exist.")),
    )

    assert rag_service._sync_collection_count("p1") == 0


def test_collection_count_raises_on_transient_chroma_error(tmp_path, monkeypatch):
    """A transient Chroma failure must reach the caller instead of being
    normalized to 'empty collection': the sweep's self-heal would treat that
    as a vanished index and re-enqueue the whole library for re-embedding."""
    _stub_chroma_env(
        tmp_path, monkeypatch,
        _StubChromaClient(error=RuntimeError("database is locked")),
    )

    with pytest.raises(RuntimeError, match="database is locked"):
        rag_service._sync_collection_count("p1")


def test_collection_doc_ids_raises_on_transient_chroma_error(tmp_path, monkeypatch):
    _stub_chroma_env(
        tmp_path, monkeypatch,
        _StubChromaClient(error=RuntimeError("database is locked")),
    )

    with pytest.raises(RuntimeError, match="database is locked"):
        rag_service._sync_collection_doc_ids("p1")


async def test_sweep_does_not_reenqueue_on_transient_count_error(project, monkeypatch):
    """End to end: a transient count failure must be one logged warning, not
    a mass re-enqueue of completed documents."""
    await _create_doc(project, content="indexable body")

    async def transient_failure(_project_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(rag_service, "collection_count", transient_failure)

    recovered = await bts.background_task_service._scan_library_project(project)

    assert recovered == 0
    assert await _claim_next(project) is None
