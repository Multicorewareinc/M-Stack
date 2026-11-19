"""The search API: authentication, the response shape, and how a missing
dependency is reported."""

from __future__ import annotations

import pytest
from conftest import AUTH, KEY, FixedEmbedder
from embeddings import EmbeddingError
from main import create_app
from store import StoreError

from settings import Settings


def test_search_returns_passages_their_sources_and_the_prompt_text(client_factory):
    response = client_factory().post("/v1/search", json={"query": "gateway replicas"}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    (passage,) = body["passages"]
    assert (
        passage["path"] == "multistack/gateway/spec.py" and passage["text"] == "replicas: int = 2"
    )
    assert passage["capability"] == "gateway"
    assert "replicas: int = 2" in body["rendered"] and body["skipped"] is None


def test_a_skipped_question_says_why(client_factory):
    client = client_factory()
    client.app.state.settings.min_query_terms = 5
    body = client.post("/v1/search", json={"query": "hi"}, headers=AUTH).json()
    assert body["passages"] == [] and body["rendered"] == "" and body["skipped"] == "few_terms"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": KEY}])
def test_search_needs_the_service_key(client_factory, headers):
    response = client_factory().post("/v1/search", json={"query": "x"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "unauthorized"


def test_an_unset_key_fails_closed(client_factory):
    response = client_factory(key="").post("/v1/search", json={"query": "x"}, headers=AUTH)
    assert response.status_code == 500


def test_an_empty_or_huge_query_is_rejected(client_factory):
    client = client_factory()
    assert client.post("/v1/search", json={"query": ""}, headers=AUTH).status_code == 422
    assert client.post("/v1/search", json={"query": "x" * 5000}, headers=AUTH).status_code == 422


def test_nothing_indexed_is_503(client_factory):
    client = client_factory(index=False)
    assert client.post("/v1/search", json={"query": "x"}, headers=AUTH).status_code == 503


class DownEmbedder(FixedEmbedder):
    def embed_query(self, question):
        raise EmbeddingError("embedding server unreachable")


def test_an_embedding_server_that_is_down_is_503(client_factory):
    client = client_factory(embedder=DownEmbedder())
    response = client.post("/v1/search", json={"query": "gateway replicas"}, headers=AUTH)
    assert response.status_code == 503 and "unreachable" in response.json()["error"]["message"]


def test_a_database_that_is_down_is_503(client_factory, memory_store):
    client = client_factory(memory_store)

    def down(*_args):
        raise StoreError("could not reach the vector database")

    memory_store.search = down
    assert (
        client.post("/v1/search", json={"query": "gateway replicas"}, headers=AUTH).status_code
        == 503
    )


def test_status_reports_what_the_index_was_built_from(client_factory):
    body = client_factory().get("/v1/status", headers=AUTH).json()
    assert (body["model"], body["chunks"], body["commit"]) == ("m", 1, "abc")
    assert "terms" not in body


def test_searches_wait_less_on_the_embedding_server_than_indexing_does():
    settings = Settings(service_api_key=KEY, embedding_url="http://e", embedding_model="m")
    app = create_app(settings)
    assert (
        app.state.searcher.embedder.config.timeout_seconds
        == settings.search_embedding_timeout_seconds
    )
    assert settings.search_embedding_timeout_seconds < settings.embedding_timeout_seconds


class OneScoreReranker:
    def rerank(self, query, documents):
        return [0.9 for _ in documents]


class DownReranker:
    def rerank(self, query, documents):
        from reranker import RerankError

        raise RerankError("down")


def test_a_reranked_search_says_so_and_carries_the_rerank_score(client_factory):
    body = (
        client_factory(reranker=OneScoreReranker())
        .post("/v1/search", json={"query": "gateway replicas"}, headers=AUTH)
        .json()
    )
    assert body["reranked"] is True and body["passages"][0]["rerank_score"] == 0.9


def test_without_a_reranker_the_order_is_the_hybrid_one(client_factory):
    body = (
        client_factory().post("/v1/search", json={"query": "gateway replicas"}, headers=AUTH).json()
    )
    assert body["reranked"] is False and body["passages"][0]["rerank_score"] is None


def test_a_down_reranker_still_answers(client_factory):
    response = client_factory(reranker=DownReranker()).post(
        "/v1/search", json={"query": "gateway replicas"}, headers=AUTH
    )
    assert response.status_code == 200 and response.json()["reranked"] is False
    assert response.json()["passages"]


def test_the_app_builds_a_reranker_only_when_one_is_configured():
    base = {"service_api_key": KEY, "embedding_url": "http://e", "embedding_model": "m"}
    assert create_app(Settings(**base)).state.searcher.reranker is None
    configured = create_app(Settings(**base, rerank_url="http://r/v1/rerank", rerank_model="rr"))
    assert configured.state.searcher.reranker.config.model == "rr"
