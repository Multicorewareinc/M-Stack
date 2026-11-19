"""The Qdrant client: the requests it sends, and how it reports failure.
The live behaviour is covered by integration/test_qdrant_integration.py."""

from __future__ import annotations

import json

import httpx
import pytest
from store import QdrantStore, StoreError


def store_with(handler) -> tuple[QdrantStore, list]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(base_url="http://q", transport=httpx.MockTransport(record))
    return QdrantStore("http://q", "ref", client=client), seen


def test_a_build_collection_is_created_with_both_vectors_and_the_lookup_indexes():
    store, seen = store_with(lambda r: httpx.Response(200, json={"result": True}))
    store.create_collection("ref_b1", 1024)
    assert seen[0].url.path == "/collections/ref_b1"
    created = json.loads(seen[0].content)
    assert created["vectors"]["dense"] == {"size": 1024, "distance": "Cosine"}
    assert created["sparse_vectors"]["keywords"] == {"modifier": "idf"}
    indexed = [json.loads(r.content) for r in seen[1:]]
    assert indexed == [
        {"field_name": name, "field_schema": "keyword"}
        for name in ("capability", "field_name", "origin", "kind")
    ]


def test_matching_reads_by_payload_with_no_vectors_and_follows_pages():
    pages = iter(
        [
            {"result": {"points": [{"id": "a", "payload": {"x": 1}}], "next_page_offset": "a2"}},
            {"result": {"points": [{"id": "b", "payload": {"x": 2}}], "next_page_offset": None}},
        ]
    )
    store, seen = store_with(lambda r: httpx.Response(200, json=next(pages)))
    hits = store.matching({"origin": "sdk", "capability": ["gateway", "valkey"]})
    assert [h.id for h in hits] == ["a", "b"]
    first = json.loads(seen[0].content)
    assert seen[0].url.path == "/collections/ref/points/scroll"
    assert first["with_vector"] is False
    assert first["filter"]["must"] == [
        {"key": "origin", "match": {"value": "sdk"}},
        {"key": "capability", "match": {"any": ["gateway", "valkey"]}},
    ]
    assert json.loads(seen[1].content)["offset"] == "a2"


def test_search_conditions_narrow_both_rankings():
    store, seen = store_with(lambda r: httpx.Response(200, json={"result": [{"points": []}] * 2}))
    store.search([0.1], {"indices": [5], "values": [1.0]}, 50, conditions={"origin": "service"})
    searches = json.loads(seen[0].content)["searches"]
    assert all(
        s["filter"] == {"must": [{"key": "origin", "match": {"value": "service"}}]} for s in searches
    )


def qdrant(*, aliases=(), collections=()):
    """A handler answering the alias and collection listings, and True to
    anything else."""

    def handler(request):
        if request.url.path == "/aliases":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "aliases": [{"alias_name": a, "collection_name": c} for a, c in aliases]
                    }
                },
            )
        if request.url.path == "/collections" and request.method == "GET":
            return httpx.Response(
                200, json={"result": {"collections": [{"name": c} for c in collections]}}
            )
        return httpx.Response(200, json={"result": True})

    return handler


def test_current_follows_the_alias():
    store, _ = store_with(qdrant(aliases=[("ref", "ref_b2")], collections=["ref_b2", "ref_meta"]))
    assert store.current() == "ref_b2"


def test_current_finds_an_index_built_before_aliases():
    store, _ = store_with(qdrant(collections=["ref", "ref_meta"]))
    assert store.current() == "ref"


def test_current_is_none_with_nothing_built():
    store, _ = store_with(qdrant(collections=["ref_meta"]))
    assert store.current() is None


def test_switching_an_existing_alias_is_one_atomic_request():
    store, seen = store_with(qdrant(aliases=[("ref", "ref_b1")], collections=["ref_b1"]))
    store.point_to("ref_b2")
    (switch,) = [r for r in seen if r.url.path == "/collections/aliases"]
    assert json.loads(switch.content) == {
        "actions": [
            {"delete_alias": {"alias_name": "ref"}},
            {"create_alias": {"collection_name": "ref_b2", "alias_name": "ref"}},
        ]
    }
    assert not any(r.method == "DELETE" for r in seen)


def test_the_first_switch_replaces_a_pre_alias_collection_of_the_same_name():
    store, seen = store_with(qdrant(collections=["ref", "ref_b1"]))
    store.point_to("ref_b1")
    methods = [(r.method, r.url.path) for r in seen if r.method in ("DELETE", "POST")]
    assert methods == [("DELETE", "/collections/ref"), ("POST", "/collections/aliases")]


def test_builds_are_the_collections_named_after_the_index_except_its_metadata():
    store, _ = store_with(qdrant(collections=["ref", "ref_meta", "ref_b1", "ref_b2", "other"]))
    assert store.builds() == ["ref_b1", "ref_b2"]


def test_an_unreachable_database_is_a_store_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    store, _ = store_with(handler)
    with pytest.raises(StoreError, match="could not reach"):
        store.read_meta()
    assert store.ready() is False


def test_a_rejected_request_is_a_store_error_naming_the_status():
    store, _ = store_with(lambda r: httpx.Response(400, text="bad vector"))
    with pytest.raises(StoreError, match="400"):
        store.upsert("ref_b1", [{"id": "x"}])


def test_search_sends_both_rankings_in_one_request():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "result": [
                    {"points": [{"id": "a", "score": 0.9, "payload": {"t": 1}}]},
                    {"points": [{"id": "b", "score": 3.2, "payload": {"t": 2}}]},
                ]
            },
        )

    store, seen = store_with(handler)
    dense, keywords = store.search([0.1, 0.2], {"indices": [5], "values": [1.0]}, 50)
    assert len(seen) == 1 and seen[0].url.path == "/collections/ref/points/query/batch"
    body = json.loads(seen[0].content)
    assert [s["using"] for s in body["searches"]] == ["dense", "keywords"]
    assert (dense[0].id, keywords[0].id) == ("a", "b")


def test_a_question_with_no_words_skips_the_word_search():
    store, seen = store_with(lambda r: httpx.Response(200, json={"result": [{"points": []}]}))
    _, keywords = store.search([0.1], {"indices": [], "values": []}, 50)
    assert len(json.loads(seen[0].content)["searches"]) == 1 and keywords == []


def test_metadata_absent_reads_as_none():
    store, _ = store_with(lambda r: httpx.Response(404, json={}))
    assert store.read_meta() is None
