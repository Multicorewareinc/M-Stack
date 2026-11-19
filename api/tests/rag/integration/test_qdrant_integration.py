"""Qdrant integration test -- the one place the collection layout, the IDF-weighted sparse
scoring and the metadata round trip run against a real vector database. NOT part of the
offline `testpaths` (that suite uses tests/conftest.py's MemoryStore); runs only against a
real Qdrant.

Preconditions: a Qdrant reachable at $QDRANT_TEST_URL, for example
`docker run -p 6333:6333 qdrant/qdrant:v1.19.1`. The test creates its own throwaway
collection and deletes it afterwards.

Run: `python -m pytest api/tests/rag/integration -q` with QDRANT_TEST_URL set.
Skips (does not fail) when QDRANT_TEST_URL is absent, so it never breaks the offline suite.
"""

from __future__ import annotations

import os
import uuid
from typing import ClassVar

import pytest
from corpus import Chunk
from indexer import build_index
from service import Searcher
from store import QdrantStore

from settings import Settings

URL = os.environ.get("QDRANT_TEST_URL")
pytestmark = pytest.mark.skipif(not URL, reason="QDRANT_TEST_URL not set — Qdrant integration only")


class Embedder:
    vectors: ClassVar[dict[str, list[float]]] = {
        "event_backbone_url: str = ''": [1.0, 0.0, 0.0],
        "replicas: int = 2": [0.0, 1.0, 0.0],
        "cache_url: str": [0.0, 0.0, 1.0],
    }

    def embed_documents(self, chunks, *, on_batch=None):
        return [self.vectors[c.text] for c in chunks]

    def embed_query(self, question):
        return [0.0, 1.0, 0.0]


@pytest.fixture
def store():
    s = QdrantStore(URL, f"test_{uuid.uuid4().hex[:10]}")
    yield s
    for name in [*s.builds(), s.meta_collection]:
        s.drop(name)


def chunks():
    return [
        Chunk(
            id=f"f.py:{n}:field",
            text=text,
            context=f"field {n}",
            capability="gateway",
            source_path="f.py",
            start_line=n,
            end_line=n,
            kind="field",
        )
        for n, text in enumerate(Embedder.vectors, start=1)
    ]


def test_build_search_and_rebuild_against_a_real_server(store):
    stats = build_index(chunks(), store, Embedder(), model="m", dimension=3, commit="abc")
    assert stats.embedded == 3 and store.count() == 3
    assert store.read_meta()["commit"] == "abc"

    searcher = Searcher(store, Embedder(), Settings(min_score=0.0, min_query_terms=0))
    result = searcher.search("what is event_backbone_url")
    texts = [p.text for p in result.passages]
    # The exact name wins; the chunk only meaning points at is still returned.
    assert texts[0] == "event_backbone_url: str = ''" and "replicas: int = 2" in texts
    assert result.best_similarity == pytest.approx(1.0)

    first = store.current()
    again = build_index(chunks()[:2], store, Embedder(), model="m", dimension=3)
    assert (again.reused, again.embedded, again.removed) == (2, 0, 1) and store.count() == 2
    # Searches moved to the new build through the alias; the old one is gone.
    assert store.current() != first and store.builds() == [store.current()]
    assert store.read_meta()["collection"] == store.current()
