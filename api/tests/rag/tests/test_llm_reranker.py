"""The chat-model ranking step: what it sends, how its answer is read, and
how a search uses it -- and falls back without it."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest
from corpus import Chunk
from indexer import build_index
from llm_reranker import LLMRerankConfig, LLMReranker, LLMRerankError
from service import Searcher

from settings import Settings


@dataclass(frozen=True)
class Candidate:
    path: str
    start: int
    end: int
    context: str
    text: str


CANDIDATES = [Candidate(f"f{i}.py", i, i, f"context {i}", f"text {i}") for i in range(4)]


def model_with(handler, **config) -> tuple[LLMReranker, list]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    config = {"url": "http://llm/v1/chat/completions", "model": "m", **config}
    return LLMReranker(LLMRerankConfig(**config), client=client), seen


def says(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_the_useful_passages_come_back_as_indices_most_useful_first():
    model, _ = model_with(lambda r: says('{"useful": [3, 1]}'))
    assert model.useful("q", CANDIDATES) == [2, 0]


def test_json_inside_prose_is_found():
    model, _ = model_with(lambda r: says('Passages 2 and 4 help.\n{"useful": [2, 4]}'))
    assert model.useful("q", CANDIDATES) == [1, 3]


def test_numbers_outside_the_list_and_repeats_are_dropped():
    model, _ = model_with(lambda r: says('{"useful": [2, 9, 0, 2, "3", true, 4]}'))
    assert model.useful("q", CANDIDATES) == [1, 3]


def test_none_useful_is_an_empty_list_not_an_error():
    model, _ = model_with(lambda r: says('{"useful": []}'))
    assert model.useful("q", CANDIDATES) == []


def test_the_request_shows_each_passage_numbered_with_its_source_and_cut_to_length():
    model, seen = model_with(lambda r: says('{"useful": [1]}'), max_passage_chars=3, subject="Acme")
    model.useful("which port", CANDIDATES)
    body = json.loads(seen[0].content)
    prompt = body["messages"][0]["content"]
    assert body["model"] == "m" and body["reasoning_effort"] == "low"
    assert "Question: which port" in prompt and "about Acme" in prompt
    assert "[1] f0.py:0-0 (context 0)\ntex\n" in prompt
    assert "[4] f3.py:3-3 (context 3)" in prompt


def test_no_candidates_means_no_request():
    model, seen = model_with(lambda r: says('{"useful": [1]}'))
    assert model.useful("q", []) == [] and seen == []


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        says("I can't decide."),
        says('{"useful": "1, 2"}'),
    ],
)
def test_an_unreadable_answer_is_an_error(reply):
    model, _ = model_with(lambda r: reply)
    with pytest.raises(LLMRerankError):
        model.useful("q", CANDIDATES)


def test_an_unreachable_model_is_an_error():
    def fail(request):
        raise httpx.ConnectError("refused")

    model, _ = model_with(fail)
    with pytest.raises(LLMRerankError, match="could not reach"):
        model.useful("q", CANDIDATES)


# ---- in a search

DIM = 8


def one_hot(i: int) -> list[float]:
    return [1.0 if j == i else 0.0 for j in range(DIM)]


def chunk(n: int, text: str) -> Chunk:
    return Chunk(
        f"src/f{n}.py:{n}:field", text, f"context {n}", "gateway", f"src/f{n}.py", n, n + 1, "field"
    )


class FixedEmbedder:
    def __init__(self, vectors):
        self.vectors = vectors

    def embed_documents(self, chunks, *, on_batch=None):
        return [self.vectors[c.text] for c in chunks]

    def embed_query(self, question):
        return one_hot(0)


class ChoosingModel:
    """Answers with fixed passage texts, in the order given, or fails."""

    def __init__(self, choose: list[str] | None) -> None:
        self.choose = choose
        self.asked: list[list[str]] = []

    def useful(self, question, candidates):
        self.asked.append([c.text for c in candidates])
        if self.choose is None:
            raise LLMRerankError("down")
        texts = [c.text for c in candidates]
        return [texts.index(t) for t in self.choose if t in texts]


def searcher(store, model, **settings) -> Searcher:
    pairs = [(chunk(i, f"needle {i}"), [1.0 - i / 100] + [0.0] * (DIM - 1)) for i in range(6)]
    embedder = FixedEmbedder({c.text: v for c, v in pairs})
    build_index([c for c, _ in pairs], store, embedder, model="m", dimension=DIM)
    config = {"min_score": 0.0, "min_query_terms": 0, "stub_penalty": 1.0, **settings}
    return Searcher(store, embedder, Settings(**config), None, model)


def test_the_models_useful_passages_lead_and_the_rest_follow_in_order(memory_store):
    model = ChoosingModel(["needle 4", "needle 2"])
    result = searcher(memory_store, model, top_k=4).search("needle")
    assert result.llm_reranked
    assert [p.text for p in result.passages] == ["needle 4", "needle 2", "needle 0", "needle 1"]


def test_the_model_sees_the_best_llm_rerank_candidates(memory_store):
    model = ChoosingModel([])
    searcher(memory_store, model, top_k=2, llm_rerank_candidates=5).search("needle")
    assert model.asked == [[f"needle {i}" for i in range(5)]]


def test_a_failing_model_leaves_the_order_as_it_was(memory_store):
    with_model = searcher(memory_store, ChoosingModel(None), top_k=3).search("needle")
    assert not with_model.llm_reranked
    assert [p.text for p in with_model.passages] == ["needle 0", "needle 1", "needle 2"]


def test_without_a_model_nothing_changes(memory_store):
    result = searcher(memory_store, None, top_k=3).search("needle")
    assert not result.llm_reranked
    assert [p.text for p in result.passages] == ["needle 0", "needle 1", "needle 2"]


def test_only_the_useful_passages_are_sent_when_asked(memory_store):
    model = ChoosingModel(["needle 4", "needle 2", "needle 5", "needle 1"])
    result = searcher(memory_store, model, top_k=6, llm_rerank_only_useful=True).search("needle")
    assert [p.text for p in result.passages] == ["needle 4", "needle 2", "needle 5", "needle 1"]


def test_too_few_useful_passages_are_topped_up_to_the_floor(memory_store):
    model = ChoosingModel(["needle 3"])
    result = searcher(
        memory_store, model, top_k=6, llm_rerank_only_useful=True, llm_rerank_min_passages=3
    ).search("needle")
    assert [p.text for p in result.passages] == ["needle 3", "needle 0", "needle 1"]
