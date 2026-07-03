from types import SimpleNamespace

import pytest

from app.core.config import Settings, settings
from app.core.exceptions import RAGIndexModelMismatchError
from app.core.model_config import ModelEndpoint
from app.services.rag_service import RAGService


class FakeIndex:
    def __init__(self, count: int):
        self.count = count
        self.requested_top_k = None

    def as_retriever(self, similarity_top_k: int):
        self.requested_top_k = similarity_top_k
        return FakeRetriever(similarity_top_k)


class FakeRetriever:
    def __init__(self, count: int):
        self.count = count

    def retrieve(self, query_bundle):
        return [
            SimpleNamespace(
                node=SimpleNamespace(metadata={"doc_id": f"doc-{i}"}, text=f"chunk {i}"),
                score=1.0 - (i * 0.1),
            )
            for i in range(self.count)
        ]


class FakeReranker:
    def __init__(self, scores=None, error=None):
        self.scores = scores or []
        self.error = error

    def predict(self, pairs):
        if self.error:
            raise self.error
        return self.scores[:len(pairs)]


class SearchOnlyRAGService(RAGService):
    def __init__(self, index, reranker=None):
        super().__init__()
        self._initialized = True
        self._reranker = reranker
        self._state = SimpleNamespace(index=index)

    def _get_project(self, project_id):
        return self._state

    def _chunk_count(self, state):
        return 100


@pytest.fixture(autouse=True)
def candidate_pool(monkeypatch):
    monkeypatch.setattr(settings.library, "candidate_pool_size", 4)


def test_search_without_reranker_returns_candidate_pool_size():
    index = FakeIndex(count=4)
    service = SearchOnlyRAGService(index=index)

    results = service._sync_search("project", "query", top_k=2)

    assert index.requested_top_k == 4
    assert [result.doc_id for result in results] == ["doc-0", "doc-1", "doc-2", "doc-3"]


def test_search_with_reranker_returns_top_k_after_rerank():
    index = FakeIndex(count=4)
    reranker = FakeReranker(scores=[0.1, 0.9, 0.2, 0.3])
    service = SearchOnlyRAGService(index=index, reranker=reranker)

    results = service._sync_search("project", "query", top_k=2)

    assert index.requested_top_k == 4
    assert [result.doc_id for result in results] == ["doc-1", "doc-3"]


def test_search_returns_candidate_pool_size_when_reranker_fails():
    index = FakeIndex(count=4)
    reranker = FakeReranker(error=RuntimeError("rerank unavailable"))
    service = SearchOnlyRAGService(index=index, reranker=reranker)

    results = service._sync_search("project", "query", top_k=2)

    assert index.requested_top_k == 4
    assert [result.doc_id for result in results] == ["doc-0", "doc-1", "doc-2", "doc-3"]


def test_candidate_pool_size_is_never_smaller_than_top_k(monkeypatch):
    monkeypatch.setattr(settings.library, "candidate_pool_size", 1)
    index = FakeIndex(count=3)
    service = SearchOnlyRAGService(index=index)

    results = service._sync_search("project", "query", top_k=3)

    assert index.requested_top_k == 3
    assert len(results) == 3


def test_bm25_search_reuses_cached_chunk_tokens():
    service = RAGService()
    service._initialized = True
    doc_tokenizations = 0

    class FakeNode:
        def __init__(self, doc_id, text):
            self.metadata = {"doc_id": doc_id, "line_start": 0}
            self._text = text

        def get_content(self):
            return self._text

    def tokenize(text):
        nonlocal doc_tokenizations
        if text.startswith("doc "):
            doc_tokenizations += 1
        return text.lower().split()

    service._tokenize_for_bm25 = tokenize
    state = SimpleNamespace(
        all_nodes=[
            FakeNode("doc-1", "doc alpha target"),
            FakeNode("doc-2", "doc beta target"),
        ],
        bm25_index=None,
    )

    first = service._bm25_search(state, "target", fetch_k=2)
    second = service._bm25_search(state, "target", fetch_k=2)

    assert [chunk.doc_id for chunk in first] == ["doc-1", "doc-2"]
    assert [chunk.doc_id for chunk in second] == ["doc-1", "doc-2"]
    assert doc_tokenizations == 2

    service._invalidate_bm25(state)
    service._bm25_search(state, "target", fetch_k=2)
    assert doc_tokenizations == 4


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


def test_extract_chunk_identity_uses_node_content_when_doc_id_is_none_string():
    meta = {
        "doc_id": "None",
        "doc_revision": None,
        "_node_content": '{"metadata": {"doc_id": "doc-1", "doc_revision": 7}}',
    }

    assert RAGService._extract_chunk_identity(meta) == ("doc-1", 7)
