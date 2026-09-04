"""Query-time retrieval behavior of RAGService:

- candidate pool sizing (over-fetch) and reranker fan-out/fallback
- the BM25 chunk-token cache (tokenize once per chunk, invalidate on change)

Index-side behavior (metadata guard, chunk identity, generation-scoped
purge/sync cleanup) lives in ``test_rag_index_maintenance.py``.
"""

import threading
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.rag_service import RAGService


# ---------------------------------------------------------------------------
# Candidate pool sizing and reranker fan-out
# ---------------------------------------------------------------------------

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
        # BM25 attributes so _sync_search takes the healthy no-BM25-hits path
        # instead of raising AttributeError inside _bm25_search and quietly
        # riding the swallowed "BM25 search failed" fallback.
        self._state = SimpleNamespace(
            index=index,
            all_nodes=[],
            bm25_index=None,
            all_nodes_lock=threading.Lock(),
        )

    def _get_project(self, project_id):
        return self._state

    def _chunk_count(self, state):
        return 100


@pytest.fixture(autouse=True)
def no_silent_bm25_failure(caplog):
    """_sync_search degrades BM25 errors to a warning. These tests exercise
    the candidate/rerank contract on the healthy path, so the warning must
    never fire — otherwise a BM25 wiring regression would pass unnoticed."""
    yield
    assert "BM25 search failed" not in caplog.text


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


# ---------------------------------------------------------------------------
# BM25 chunk-token cache
# ---------------------------------------------------------------------------

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
        all_nodes_lock=threading.Lock(),
    )

    first = service._bm25_search(state, "target", fetch_k=2)
    second = service._bm25_search(state, "target", fetch_k=2)

    assert [chunk.doc_id for chunk in first] == ["doc-1", "doc-2"]
    assert [chunk.doc_id for chunk in second] == ["doc-1", "doc-2"]
    assert doc_tokenizations == 2

    service._invalidate_bm25(state)
    service._bm25_search(state, "target", fetch_k=2)
    assert doc_tokenizations == 4
