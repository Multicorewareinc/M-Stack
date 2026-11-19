"""The reranker client: what it sends, and which replies it refuses."""

from __future__ import annotations

import json

import httpx
import pytest
from reranker import RerankConfig, Reranker, RerankError


def reranker_with(handler, **config) -> tuple[Reranker, list]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return Reranker(
        RerankConfig(url="http://r/v1/rerank", model="m", **config), client=client
    ), seen


def results(*pairs):
    return httpx.Response(
        200, json={"results": [{"index": i, "relevance_score": s} for i, s in pairs]}
    )


def test_scores_come_back_in_the_order_the_documents_were_sent():
    # The server lists results best first; the client returns them by index.
    r, _ = reranker_with(lambda req: results((1, 0.9), (0, 0.2), (2, 0.5)))
    assert r.rerank("q", ["a", "b", "c"]) == [0.2, 0.9, 0.5]


def test_the_request_is_the_rerank_api_shape():
    r, seen = reranker_with(lambda req: results((0, 0.1)))
    r.rerank("which field", ["doc"])
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/rerank"
    assert body == {"model": "m", "query": "which field", "documents": ["doc"]}


def test_templates_wrap_the_query_and_each_document():
    r, seen = reranker_with(
        lambda req: results((0, 0.1)),
        query_template="<Query>: {query}",
        document_template="<Document>: {document}<end>",
    )
    r.rerank("q", ["d"])
    body = json.loads(seen[0].content)
    assert body["query"] == "<Query>: q" and body["documents"] == ["<Document>: d<end>"]


def test_long_documents_are_cut_before_sending():
    r, seen = reranker_with(lambda req: results((0, 0.1)), max_document_chars=10)
    r.rerank("q", ["x" * 50])
    assert json.loads(seen[0].content)["documents"] == ["x" * 10]


def test_nothing_to_rerank_makes_no_request():
    r, seen = reranker_with(lambda req: pytest.fail("no request expected"))
    assert r.rerank("q", []) == [] and seen == []


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"no": "results"}),
        httpx.Response(200, json={"results": [{"index": 0}]}),
        results((0, 0.5)),  # one score for two documents
        results((0, 0.5), (5, 0.1)),  # an index that was never sent
    ],
)
def test_a_reply_that_cant_be_trusted_is_an_error(reply):
    r, _ = reranker_with(lambda req: reply)
    with pytest.raises(RerankError):
        r.rerank("q", ["a", "b"])


def test_an_unreachable_reranker_is_an_error():
    def down(req):
        raise httpx.ConnectError("refused")

    r, _ = reranker_with(down)
    with pytest.raises(RerankError, match="could not reach"):
        r.rerank("q", ["a"])
