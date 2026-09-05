"""Index-side maintenance behavior of RAGService:

- the embedding-identity metadata guard persisted beside the Chroma store
- chunk identity normalization shared by every cleanup path
- generation-scoped index writes and purge (a doc's chunks are replaced or
  removed only for the exact (doc_id, revision, generation) triple, so a
  partial write can never destroy newer chunks)

Query-time behavior (candidate pool, rerank, BM25 cache) lives in
``test_rag_search_candidates.py``.
"""

import threading
import signal
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.exceptions import RAGIndexModelMismatchError
from app.core.model_config import ModelEndpoint
from app.services import rag_service as rag_service_module
from app.services.rag_service import RAGService


class _StuckWorker:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.join_calls = []

    def is_alive(self):
        return not self.killed

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def join(self, timeout):
        self.join_calls.append(timeout)


@pytest.mark.asyncio
async def test_cancelled_rag_worker_is_force_killed_and_reaped(monkeypatch):
    service = RAGService()
    worker = _StuckWorker()
    monkeypatch.setattr(
        "app.services.rag_service.settings",
        SimpleNamespace(RAG_INDEX_TERMINATE_GRACE_SECONDS=0.01),
    )

    await service._stop_index_worker(worker)

    assert worker.terminated is True
    assert worker.killed is True
    assert worker.join_calls == [0.01, 0.01]


def test_rag_worker_signal_targets_owned_process_group(monkeypatch):
    worker = SimpleNamespace(pid=4123, terminate=lambda: None, kill=lambda: None)
    signals = []
    monkeypatch.setattr(rag_service_module.os, "name", "posix")
    monkeypatch.setattr(rag_service_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        rag_service_module.os,
        "killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    RAGService._signal_index_worker(worker, signal.SIGTERM)

    assert signals == [(4123, signal.SIGTERM)]


def test_rag_worker_configures_parent_death_signal(monkeypatch):
    calls = []

    class FakeLibC:
        def prctl(self, option, sig, arg2, arg3, arg4):
            calls.append((option, sig, arg2, arg3, arg4))
            return 0

    monkeypatch.setattr(rag_service_module.os, "name", "posix")
    monkeypatch.setattr(rag_service_module.sys, "platform", "linux")
    monkeypatch.setattr(rag_service_module.os, "getppid", lambda: 4000)
    monkeypatch.setattr(rag_service_module.os, "setsid", lambda: calls.append("setsid"))
    monkeypatch.setattr(rag_service_module.ctypes, "CDLL", lambda *args, **kwargs: FakeLibC())

    rag_service_module._configure_index_worker_lifecycle()

    assert calls == [
        "setsid",
        (rag_service_module._PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0),
    ]


# ---------------------------------------------------------------------------
# Embedding-identity metadata guard on the Chroma collection
# ---------------------------------------------------------------------------

class FakeCollection:
    def __init__(self, count=0, metadata=None):
        self._count = count
        self.metadata = metadata or {}
        self.modified_metadata = None

    def count(self):
        return self._count

    def modify(self, metadata):
        self.modified_metadata = metadata
        self.metadata = metadata


def _identity(model: str = "embed-a") -> dict:
    endpoint = ModelEndpoint(role="embedding", model=model)
    return RAGService._build_embedding_identity(endpoint, query_instruction=None)


def test_rag_metadata_written_for_empty_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "get_sigma_path", lambda self, project_id: tmp_path)
    service = RAGService()
    service._embedding_identity = _identity("embed-a")
    collection = FakeCollection(count=0)

    service._ensure_index_metadata("project", collection)

    assert (tmp_path / "rag_index_metadata.json").exists()
    assert collection.metadata["embedding_model"] == "embed-a"


def test_rag_metadata_rejects_nonempty_unknown_index(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "get_sigma_path", lambda self, project_id: tmp_path)
    service = RAGService()
    service._embedding_identity = _identity("embed-a")

    with pytest.raises(RAGIndexModelMismatchError):
        service._ensure_index_metadata("project", FakeCollection(count=1))


def test_rag_metadata_rejects_changed_embedding_model(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "get_sigma_path", lambda self, project_id: tmp_path)
    service = RAGService()
    service._embedding_identity = _identity("embed-a")
    service._write_index_metadata("project")

    service._embedding_identity = _identity("embed-b")
    with pytest.raises(RAGIndexModelMismatchError):
        service._ensure_index_metadata("project", FakeCollection(count=1))


# ---------------------------------------------------------------------------
# Chunk identity normalization (metadata shapes seen in real collections)
# ---------------------------------------------------------------------------

def test_extract_chunk_identity_uses_node_content_when_doc_id_is_none_string():
    meta = {
        "doc_id": "None",
        "doc_revision": None,
        "_node_content": '{"metadata": {"doc_id": "doc-1", "doc_revision": 7}}',
    }

    assert RAGService._extract_chunk_identity(meta) == ("doc-1", 7, 0)


def test_extract_chunk_identity_normalizes_legacy_generation_zero():
    assert RAGService._extract_chunk_identity({
        "doc_id": "doc-1",
        "doc_revision": 7,
    }) == ("doc-1", 7, 0)


# ---------------------------------------------------------------------------
# Generation-scoped writes and purge
# ---------------------------------------------------------------------------

class _IndexNode:
    def __init__(self, text):
        self._text = text
        self.metadata = {}
        self.relationships = {}
        self.node_id = "node"

    def get_content(self):
        return self._text


class _IndexCollection:
    def __init__(self, records=None):
        self.records = dict(records or {})

    def get(self, include=None):
        return {
            "ids": list(self.records),
            "metadatas": [record["metadata"] for record in self.records.values()],
        }

    def delete(self, ids):
        for record_id in ids:
            self.records.pop(record_id, None)

    def add(self, ids, metadatas, documents, embeddings):
        for record_id, metadata, document in zip(ids, metadatas, documents):
            if record_id in self.records:
                raise ValueError("duplicate")
            self.records[record_id] = {
                "metadata": metadata,
                "document": document,
            }


class _IndexVectorStore:
    def __init__(self, collection):
        self._collection = collection

    def add(self, nodes):
        self._collection.add(
            ids=[node.node_id for node in nodes],
            metadatas=[node.metadata for node in nodes],
            documents=[node.get_content() for node in nodes],
            embeddings=[node.embedding for node in nodes],
        )


class _GenerationCollection:
    def __init__(self, records):
        self.records = dict(records)

    def get(self, include=None):
        return {
            "ids": list(self.records),
            "metadatas": [self.records[record_id] for record_id in self.records],
        }

    def delete(self, ids):
        for record_id in ids:
            self.records.pop(record_id, None)


def _index_service(monkeypatch, collection):
    service = RAGService()
    service._initialized = True
    service._md_parser = SimpleNamespace(
        get_nodes_from_documents=lambda _documents: [_IndexNode("new body")],
    )
    service._smart_chunker = lambda nodes: nodes
    service._embed_model = SimpleNamespace(
        get_text_embedding_batch=lambda texts: [[1.0, 0.0] for _ in texts],
    )
    state = SimpleNamespace(
        vector_store=_IndexVectorStore(collection),
        all_nodes=[],
        bm25_index=None,
        all_nodes_lock=threading.Lock(),
    )
    monkeypatch.setattr(service, "_ensure_init", lambda: None)
    monkeypatch.setattr(service, "_get_project", lambda _project_id: state)
    monkeypatch.setattr(
        "app.core.project_registry.is_project_active", lambda _project_id: True,
    )
    return service, state


def test_real_old_chroma_metadata_is_cleanup_compatible(tmp_path, monkeypatch):
    """Chunks written by the legacy metadata shape (no generation) must be
    matched and removed by the generation-0 purge path, using real Chroma."""
    import chromadb

    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    collection = client.get_or_create_collection(name="library")
    collection.add(
        ids=["legacy"],
        documents=["legacy content"],
        metadatas=[{"doc_id": "doc-1", "doc_revision": 7}],
        embeddings=[[1.0, 0.0]],
    )
    service = RAGService()
    state = SimpleNamespace(
        vector_store=SimpleNamespace(_collection=collection),
        all_nodes=[],
        bm25_index=None,
        all_nodes_lock=threading.Lock(),
    )

    service._purge_doc_chunks(state, "doc-1", revision=7, generation=0)

    assert collection.count() == 0


def test_prepare_add_does_not_delete_old_generation(monkeypatch):
    """A new generation's index write stages beside the old generation; the
    old chunks are purged only after the new ones are safely published."""
    collection = _IndexCollection({
        "old": {"metadata": {
            "doc_id": "doc-1", "doc_revision": 4, "index_generation": 8,
        }},
    })
    service, state = _index_service(monkeypatch, collection)

    assert service._sync_index(
        "project", "doc-1", "body", "title", "", doc_revision=5,
        index_generation=9,
    ) is True
    assert set(collection.records) == {"old", "node"}
    assert len(state.all_nodes) == 1


def test_same_generation_retry_replaces_partial_write_idempotently(monkeypatch):
    """Retrying the same generation replaces its partial chunks in place
    instead of duplicating or tripping over the existing ids."""
    collection = _IndexCollection()
    service, state = _index_service(monkeypatch, collection)

    service._sync_index(
        "project", "doc-1", "body", "title", "", doc_revision=5,
        index_generation=9,
    )
    service._sync_index(
        "project", "doc-1", "body", "title", "", doc_revision=5,
        index_generation=9,
    )

    assert set(collection.records) == {"node"}
    assert collection.records["node"]["metadata"]["index_generation"] == 9
    assert len(state.all_nodes) == 1


def test_failed_publish_cleanup_removes_vector_and_bm25_generation():
    """Purging the just-failed generation clears it from the vector store,
    the node cache, and drops the now-stale BM25 index entirely."""
    collection = _GenerationCollection({
        "new": {"doc_id": "doc-1", "doc_revision": 5, "index_generation": 9},
    })
    node = SimpleNamespace(
        metadata={"doc_id": "doc-1", "doc_revision": 5, "index_generation": 9},
    )
    state = SimpleNamespace(
        vector_store=SimpleNamespace(_collection=collection),
        all_nodes=[node],
        bm25_index={"entries": [node]},
        all_nodes_lock=threading.Lock(),
    )

    RAGService()._purge_doc_chunks(state, "doc-1", revision=5, generation=9)

    assert collection.records == {}
    assert state.all_nodes == []
    assert state.bm25_index is None


def test_successful_publish_cleanup_removes_old_generation_on_both_indexes():
    """After a successful publish, purging the old generation leaves only the
    new chunks in the vector store and the node cache."""
    collection = _GenerationCollection({
        "old": {"doc_id": "doc-1", "doc_revision": 4, "index_generation": 8},
        "new": {"doc_id": "doc-1", "doc_revision": 5, "index_generation": 9},
    })
    old_node = SimpleNamespace(
        metadata={"doc_id": "doc-1", "doc_revision": 4, "index_generation": 8},
    )
    new_node = SimpleNamespace(
        metadata={"doc_id": "doc-1", "doc_revision": 5, "index_generation": 9},
    )
    state = SimpleNamespace(
        vector_store=SimpleNamespace(_collection=collection),
        all_nodes=[old_node, new_node],
        bm25_index={"entries": [old_node, new_node]},
        all_nodes_lock=threading.Lock(),
    )

    RAGService()._purge_doc_chunks(state, "doc-1", revision=4, generation=8)

    assert set(collection.records) == {"new"}
    assert state.all_nodes == [new_node]
    assert state.bm25_index is None


def test_exact_generation_cleanup_never_deletes_newer_chunks():
    """A purge keyed to (doc_id, revision, generation) cannot touch chunks
    written by a newer generation."""
    collection = _GenerationCollection({
        "old": {"doc_id": "doc-1", "doc_revision": 4, "index_generation": 8},
        "new": {"doc_id": "doc-1", "doc_revision": 5, "index_generation": 9},
    })
    state = SimpleNamespace(
        vector_store=SimpleNamespace(_collection=collection),
        all_nodes=[],
        bm25_index=None,
        all_nodes_lock=threading.Lock(),
    )

    RAGService()._purge_doc_chunks(state, "doc-1", revision=4, generation=8)

    assert set(collection.records) == {"new"}
