"""The embedding client, against a fake server: what it sends, and what it
refuses to accept back."""

from __future__ import annotations

import json
import math

import httpx
import pytest
from corpus import Chunk
from embeddings import (
    DEFAULT_QUERY_INSTRUCTION,
    Embedder,
    EmbeddingConfig,
    EmbeddingError,
    cosine,
)

DIM = 1024


def make_chunk(n: int) -> Chunk:
    return Chunk(
        id=f"f.py:{n}:field",
        text=f"text-{n}",
        context=f"context-{n}",
        capability="gateway",
        source_path="f.py",
        start_line=n,
        end_line=n,
        kind="field",
    )


def one_hot(position: int, scale: float = 1.0) -> list[float]:
    vector = [0.0] * DIM
    vector[position] = scale
    return vector


def fake_server(
    requests: list[dict],
    *,
    reverse: bool = False,
    scale: float = 1.0,
    drop: int = 0,
    dim: int = DIM,
):
    """Answers each text with a vector pointing along the number in its
    'text-N' / 'context-N' label, so a test can tell which vector went
    with which chunk."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        items = []
        for i, text in enumerate(body["input"]):
            number = int(text.rsplit("-", 1)[-1]) if text.rsplit("-", 1)[-1].isdigit() else 0
            vector = (
                one_hot(number, scale)[:dim]
                if dim <= DIM
                else one_hot(number, scale) + [0.0] * (dim - DIM)
            )
            items.append({"index": i, "object": "embedding", "embedding": vector})
        if reverse:
            items.reverse()
        return httpx.Response(200, json={"data": items[: len(items) - drop]})

    return handler


def embedder(handler, **config) -> Embedder:
    cfg = EmbeddingConfig(
        url="http://embed.test/v1/embeddings", model="qwen3-embedding-0.6b", **config
    )
    return Embedder(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_documents_are_sent_as_plain_text_with_their_context_line():
    seen: list[dict] = []
    embedder(fake_server(seen)).embed_documents([make_chunk(1)])
    assert seen[0]["input"] == ["context-1\n\ntext-1"]
    assert seen[0]["model"] == "qwen3-embedding-0.6b"
    assert not seen[0]["input"][0].startswith("Instruct:")


def test_a_query_is_wrapped_in_the_instruction_the_model_expects():
    seen: list[dict] = []
    embedder(fake_server(seen)).embed_query("how do I set replicas")
    assert seen[0]["input"] == [
        f"Instruct: {DEFAULT_QUERY_INSTRUCTION}\nQuery:how do I set replicas"
    ]


def test_a_custom_instruction_replaces_the_default():
    seen: list[dict] = []
    embedder(fake_server(seen), query_instruction="Find the rule").embed_query("q")
    assert seen[0]["input"] == ["Instruct: Find the rule\nQuery:q"]


def test_another_models_wrapping_is_a_setting():
    seen: list[dict] = []
    e5 = embedder(
        fake_server(seen), query_template="query: {query}", document_template="passage: {text}"
    )
    e5.embed_query("how do I set replicas")
    e5.embed_documents([make_chunk(1)])
    assert [b["input"] for b in seen] == [
        ["query: how do I set replicas"],
        ["passage: context-1\n\ntext-1"],
    ]


def test_braces_in_the_text_are_left_as_they_are():
    seen: list[dict] = []
    embedder(fake_server(seen)).embed_query("what does {replicas} mean")
    assert seen[0]["input"][0].endswith("Query:what does {replicas} mean")


def test_a_text_longer_than_the_model_accepts_is_cut():
    seen: list[dict] = []
    embedder(fake_server(seen), max_input_tokens=10).embed_query("x" * 500)
    assert len(seen[0]["input"][0]) == 30


def test_chunks_go_out_in_sequential_batches_no_larger_than_the_limit():
    seen: list[dict] = []
    progress: list[tuple[int, int]] = []
    vectors = embedder(fake_server(seen), batch_size=32).embed_documents(
        [make_chunk(n) for n in range(70)],
        on_batch=lambda done, total: progress.append((done, total)),
    )
    assert [len(b["input"]) for b in seen] == [32, 32, 6]
    assert len(vectors) == 70
    assert progress == [(32, 70), (64, 70), (70, 70)]


def test_vectors_come_back_in_chunk_order_even_if_the_server_shuffles_them():
    vectors = embedder(fake_server([], reverse=True)).embed_documents(
        [make_chunk(n) for n in range(5)]
    )
    for n, vector in enumerate(vectors):
        assert vector[n] == pytest.approx(1.0), f"chunk {n} got someone else's vector"


def test_vectors_are_normalised_to_unit_length():
    (vector,) = embedder(fake_server([], scale=7.5)).embed_documents([make_chunk(3)])
    assert math.sqrt(sum(x * x for x in vector)) == pytest.approx(1.0)


def test_similarity_of_identical_and_unrelated_vectors():
    a, b = embedder(fake_server([])).embed_documents([make_chunk(1), make_chunk(2)])
    assert cosine(a, a) == pytest.approx(1.0)
    assert cosine(a, b) == pytest.approx(0.0)


def test_nothing_is_sent_for_an_empty_list():
    seen: list[dict] = []
    assert embedder(fake_server(seen)).embed_documents([]) == []
    assert seen == []


def test_a_short_answer_is_refused_rather_than_misaligned():
    with pytest.raises(EmbeddingError, match="sent 3 texts, got 2 vectors"):
        embedder(fake_server([], drop=1)).embed_documents([make_chunk(n) for n in range(3)])


def test_a_vector_of_the_wrong_width_is_refused():
    with pytest.raises(EmbeddingError, match="expected 1024-dimension vectors, got 768"):
        embedder(fake_server([], dim=768)).embed_documents([make_chunk(1)])


def test_an_error_status_is_reported_with_the_servers_message():
    def handler(request):
        return httpx.Response(400, text="bad body")

    with pytest.raises(EmbeddingError, match="400: bad body"):
        embedder(handler).embed_documents([make_chunk(1)])


def test_an_unreadable_answer_is_reported():
    def handler(request):
        return httpx.Response(200, text="<html>gateway timeout</html>")

    with pytest.raises(EmbeddingError, match="unreadable response"):
        embedder(handler).embed_documents([make_chunk(1)])


def test_an_unreachable_server_is_reported_with_its_address():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(
        EmbeddingError, match=r"could not reach the embedding server at http://embed\.test"
    ):
        embedder(handler).embed_documents([make_chunk(1)])


def test_config_needs_both_settings(monkeypatch):
    monkeypatch.delenv("EMBEDDING_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    with pytest.raises(EmbeddingError, match="EMBEDDING_URL and EMBEDDING_MODEL must be set"):
        EmbeddingConfig.from_env()


def test_config_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("EMBEDDING_URL", "http://x/v1/embeddings")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "8")
    monkeypatch.setenv("QUERY_INSTRUCTION", "custom")
    monkeypatch.setenv("QUERY_TEMPLATE", "query: {query}")
    monkeypatch.setenv("DOCUMENT_TEMPLATE", "passage: {text}")
    monkeypatch.setenv("EMBEDDING_MAX_INPUT_TOKENS", "512")
    config = EmbeddingConfig.from_env()
    assert (config.query_template, config.document_template, config.max_input_tokens) == (
        "query: {query}",
        "passage: {text}",
        512,
    )
    assert (config.url, config.model, config.batch_size, config.query_instruction) == (
        "http://x/v1/embeddings",
        "m",
        8,
        "custom",
    )
