import sys
from types import SimpleNamespace

from app.core.model_config import ModelEndpoint
from app.services.rag_service import LiteLLMEmbeddingModel, LiteLLMReranker


def test_litellm_embedding_orders_provider_rows(monkeypatch):
    def fake_embedding(**kwargs):
        rows = [
            {"index": i, "embedding": [float(i), float(i) + 0.1]}
            for i, _ in enumerate(kwargs["input"])
        ]
        return {"data": list(reversed(rows))}

    fake_litellm = SimpleNamespace(
        embedding=fake_embedding
    )
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    endpoint = ModelEndpoint(
        role="embedding",
        model="embedding-test",
        provider="openai",
        api_key="sk-test",
    )
    model = LiteLLMEmbeddingModel(endpoint)

    assert model.get_text_embedding_batch(["a", "b"]) == [[0.0, 0.1], [1.0, 1.1]]
    assert model.get_query_embedding("q") == [0.0, 0.1]


def test_litellm_reranker_maps_scores_by_index(monkeypatch):
    fake_litellm = SimpleNamespace(
        rerank=lambda **kwargs: {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.2},
            ]
        }
    )
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)

    endpoint = ModelEndpoint(
        role="rerank",
        model="rerank-test",
        provider="cohere",
        api_key="sk-test",
    )
    reranker = LiteLLMReranker(endpoint)

    scores = reranker.predict([["q", "doc a"], ["q", "doc b"]])

    assert scores.tolist() == [0.2, 0.9]
