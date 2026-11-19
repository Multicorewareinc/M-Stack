"""Test helpers: an in-memory stand-in for the vector database and a fixed
embedder, so tests never need Qdrant or the embedding server, and
make_client() to build the app on top of them.

MemoryStore scores the way Qdrant does: cosine for the dense vectors, and
for the sparse ones a dot product with each word weighted by
ln(1 + (N - n + 0.5) / (n + 0.5))."""

from __future__ import annotations

import math

import pytest
from corpus import Chunk
from fastapi.testclient import TestClient
from indexer import build_index
from main import create_app
from service import Searcher
from store import Hit

from settings import Settings

KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


class MemoryStore:
    """Collections by name, and one alias (`collection`) that searches go
    through, as QdrantStore has."""

    collection = "ref"

    def __init__(self) -> None:
        self.collections_: dict[str, dict[str, dict]] = {}
        self.alias: str | None = None
        self.meta: dict | None = None
        self.searches = 0
        self.fail_upsert = False

    @property
    def points(self) -> dict[str, dict]:
        """The points searches see."""
        return self.collections_.get(self.alias, {}) if self.alias else {}

    def create_collection(self, name: str, dimension: int) -> None:
        self.collections_[name] = {}

    def ensure_meta(self) -> None:
        pass

    def collections(self) -> list[str]:
        return list(self.collections_)

    def current(self) -> str | None:
        return self.alias

    def point_to(self, name: str) -> None:
        self.alias = name

    def drop(self, name: str) -> None:
        self.collections_.pop(name, None)
        if self.alias == name:
            self.alias = None

    def builds(self) -> list[str]:
        return [c for c in self.collections_ if c.startswith(f"{self.collection}_")]

    def ready(self) -> bool:
        return True

    def count(self) -> int:
        return len(self.points)

    def existing(self, name: str) -> dict:
        return {
            pid: (p["payload"]["content_hash"], p["vector"]["dense"])
            for pid, p in self.collections_.get(name, {}).items()
        }

    def upsert(self, name: str, points) -> None:
        if self.fail_upsert:
            from store import StoreError

            raise StoreError("upsert refused")
        for p in points:
            self.collections_[name][str(p["id"])] = p

    def write_meta(self, meta: dict) -> None:
        self.meta = meta

    def read_meta(self) -> dict | None:
        return self.meta

    @staticmethod
    def _meets(payload: dict, conditions) -> bool:
        return all(
            payload.get(k) == v if isinstance(v, str) else payload.get(k) in v
            for k, v in (conditions or {}).items()
        )

    def matching(self, conditions, *, limit: int = 1000) -> list[Hit]:
        return [
            Hit(pid, 0.0, p["payload"])
            for pid, p in self.points.items()
            if self._meets(p["payload"], conditions)
        ][:limit]

    def search(self, dense, sparse, pool, *, conditions=None):
        self.searches += 1
        points = {pid: p for pid, p in self.points.items() if self._meets(p["payload"], conditions)}
        dense_hits = sorted(
            (
                Hit(
                    pid,
                    sum(a * b for a, b in zip(dense, p["vector"]["dense"], strict=True)),
                    p["payload"],
                )
                for pid, p in points.items()
            ),
            key=lambda h: -h.score,
        )[:pool]
        n = len(points)
        frequency: dict[int, int] = {}
        for p in points.values():
            for i in p["vector"]["keywords"]["indices"]:
                frequency[i] = frequency.get(i, 0) + 1
        keyword_hits = []
        for pid, p in points.items():
            weights = dict(
                zip(
                    p["vector"]["keywords"]["indices"],
                    p["vector"]["keywords"]["values"],
                    strict=True,
                )
            )
            score = sum(
                q * weights[i] * math.log(1 + (n - frequency[i] + 0.5) / (frequency[i] + 0.5))
                for i, q in zip(sparse["indices"], sparse["values"], strict=True)
                if i in weights
            )
            if score > 0:
                keyword_hits.append(Hit(pid, score, p["payload"]))
        keyword_hits.sort(key=lambda h: -h.score)
        return dense_hits, keyword_hits[:pool]


@pytest.fixture
def memory_store() -> MemoryStore:
    return MemoryStore()


class FixedEmbedder:
    """Every chunk and every question points the same way."""

    def embed_documents(self, chunks, *, on_batch=None):
        return [[1.0, 0.0] for _ in chunks]

    def embed_query(self, question):
        return [1.0, 0.0]

    def embed_queries(self, questions):
        return [[1.0, 0.0] for _ in questions]


REPLICAS = Chunk(
    id="s.py:1:field",
    text="replicas: int = 2",
    context="field 'replicas' of class Gateway",
    capability="gateway",
    source_path="multistack/gateway/spec.py",
    start_line=1,
    end_line=1,
    kind="field",
)


def make_client(
    store=None,
    *,
    key: str = KEY,
    embedder=None,
    reranker=None,
    index: bool = True,
    **settings_kwargs,
):
    """The app over `store` (a fresh MemoryStore if none), with one chunk
    indexed unless `index` is False. The gate is off unless a test sets it."""
    store = store if store is not None else MemoryStore()
    embedder = embedder or FixedEmbedder()
    if index:
        build_index([REPLICAS], store, embedder, model="m", dimension=2, commit="abc")
    settings = Settings(
        service_api_key=key, **{"min_score": 0.0, "min_query_terms": 0, **settings_kwargs}
    )
    app = create_app(settings, searcher=Searcher(store, embedder, settings, reranker))
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client_factory():
    return make_client
