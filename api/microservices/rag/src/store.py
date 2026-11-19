"""The vector database (Qdrant), over its REST API.

The index is searched under one name, an alias. Each build writes a new
collection, `<name>_<build id>`, and then moves the alias onto it in one
step, so a search sees either the old index or the new one, never a mix.
Every chunk has two vectors: `dense` (the embedding, cosine) and
`keywords` (sparse, scored with inverse document frequency by the store).
A one-point collection, `<name>_meta`, records what the current build was
made from and which words occur in it.

REST through httpx rather than a client library: the service already
depends on httpx, and the calls used are a handful.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

DENSE = "dense"
KEYWORDS = "keywords"
_META_POINT = 1


class StoreError(RuntimeError):
    """The vector database could not be reached or refused a request."""


def _must(conditions: dict[str, str | Sequence[str]]) -> list[dict]:
    """Qdrant conditions: each key must hold the value, or any of a list."""
    return [
        {"key": key, "match": {"value": value} if isinstance(value, str) else {"any": list(value)}}
        for key, value in conditions.items()
    ]


@dataclass(frozen=True)
class Hit:
    id: str
    score: float
    payload: dict


class QdrantStore:
    def __init__(
        self,
        url: str,
        collection: str,
        *,
        api_key: str = "",
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.collection = collection
        self.meta_collection = f"{collection}_meta"
        headers = {"api-key": api_key} if api_key else {}
        self._client = client or httpx.Client(
            base_url=url.rstrip("/"), headers=headers, timeout=timeout
        )

    def _call(
        self, method: str, path: str, body: dict | None = None, *, missing_ok: bool = False
    ) -> Any:
        """The response's `result`: a dict, a list or a bool depending on the
        call, or None for a 404 when `missing_ok`."""
        try:
            response = self._client.request(method, path, json=body)
        except httpx.HTTPError as e:
            raise StoreError(f"could not reach the vector database: {e}") from e
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code >= 300:
            raise StoreError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        return response.json().get("result")

    # ---- collections

    def ready(self) -> bool:
        try:
            return self._client.get("/readyz").status_code == 200
        except httpx.HTTPError:
            return False

    def create_collection(self, name: str, dimension: int) -> None:
        self._call(
            "PUT",
            f"/collections/{name}",
            {
                "vectors": {DENSE: {"size": dimension, "distance": "Cosine"}},
                "sparse_vectors": {KEYWORDS: {"modifier": "idf"}},
            },
        )
        # What exact lookups filter on: a field's own chunk by name, and only
        # the SDK's or only the services' chunks of a capability.
        for field_name in ("capability", "field_name", "origin", "kind"):
            self._call(
                "PUT",
                f"/collections/{name}/index",
                {"field_name": field_name, "field_schema": "keyword"},
            )

    def ensure_meta(self) -> None:
        if self._call("GET", f"/collections/{self.meta_collection}", missing_ok=True) is None:
            self._call(
                "PUT",
                f"/collections/{self.meta_collection}",
                {"vectors": {"size": 1, "distance": "Dot"}},
            )

    def collections(self) -> list[str]:
        return [c["name"] for c in self._call("GET", "/collections")["collections"]]

    def current(self) -> str | None:
        """The collection searches go to: the one the alias points at, or,
        for an index built before aliases were used, a collection that has
        the alias's own name. None if there is neither."""
        for alias in self._call("GET", "/aliases")["aliases"]:
            if alias["alias_name"] == self.collection:
                return alias["collection_name"]
        return self.collection if self.collection in self.collections() else None

    def point_to(self, name: str) -> None:
        """Moves searches onto `name`. Atomic when the alias already exists.
        An index from before aliases is a real collection under the alias's
        name, and has to be dropped before the alias can take that name, so
        that first switch leaves a moment with nothing to search."""
        actions: list[dict] = []
        if any(
            a["alias_name"] == self.collection for a in self._call("GET", "/aliases")["aliases"]
        ):
            actions.append({"delete_alias": {"alias_name": self.collection}})
        elif self.collection in self.collections():
            self.drop(self.collection)
        actions.append({"create_alias": {"collection_name": name, "alias_name": self.collection}})
        self._call("POST", "/collections/aliases", {"actions": actions})

    def drop(self, name: str) -> None:
        self._call("DELETE", f"/collections/{name}", missing_ok=True)

    def builds(self) -> list[str]:
        """Every collection a build of this index has written."""
        prefix = f"{self.collection}_"
        return [c for c in self.collections() if c.startswith(prefix) and c != self.meta_collection]

    def count(self) -> int:
        result = self._call(
            "POST", f"/collections/{self.collection}/points/count", {"exact": True}, missing_ok=True
        )
        return result["count"] if result else 0

    # ---- writing

    def existing(self, name: str) -> dict[str, tuple[str, list[float]]]:
        """point id -> (content hash, dense vector) in collection `name`,
        for reusing vectors."""
        found: dict[str, tuple[str, list[float]]] = {}
        offset = None
        while True:
            body = {"limit": 256, "with_payload": ["content_hash"], "with_vector": [DENSE]}
            if offset is not None:
                body["offset"] = offset
            result = self._call("POST", f"/collections/{name}/points/scroll", body, missing_ok=True)
            if result is None:
                return found
            for point in result["points"]:
                found[str(point["id"])] = (
                    point["payload"].get("content_hash", ""),
                    point["vector"][DENSE],
                )
            offset = result.get("next_page_offset")
            if offset is None:
                return found

    def upsert(self, name: str, points: Sequence[dict], *, batch: int = 128) -> None:
        for start in range(0, len(points), batch):
            self._call(
                "PUT",
                f"/collections/{name}/points?wait=true",
                {"points": list(points[start : start + batch])},
            )

    def write_meta(self, meta: dict) -> None:
        self._call(
            "PUT",
            f"/collections/{self.meta_collection}/points?wait=true",
            {
                "points": [{"id": _META_POINT, "vector": [0.0], "payload": meta}],
            },
        )

    def read_meta(self) -> dict | None:
        result = self._call(
            "POST",
            f"/collections/{self.meta_collection}/points",
            {"ids": [_META_POINT], "with_payload": True},
            missing_ok=True,
        )
        return result[0]["payload"] if result else None

    # ---- searching

    def matching(
        self, conditions: dict[str, str | Sequence[str]], *, limit: int = 1000
    ) -> list[Hit]:
        """Every chunk whose payload has these values -- a value or any of a
        list -- with no vectors involved. Score 0: the order is the caller's."""
        must = _must(conditions)
        hits: list[Hit] = []
        offset = None
        while len(hits) < limit:
            body: dict = {
                "limit": min(256, limit - len(hits)),
                "with_payload": True,
                "with_vector": False,
                "filter": {"must": must},
            }
            if offset is not None:
                body["offset"] = offset
            result = self._call("POST", f"/collections/{self.collection}/points/scroll", body)
            hits += [Hit(str(p["id"]), 0.0, p["payload"]) for p in result["points"]]
            offset = result.get("next_page_offset")
            if offset is None:
                break
        return hits

    def search(
        self,
        dense: Sequence[float],
        sparse: dict,
        pool: int,
        *,
        conditions: dict[str, str | Sequence[str]] | None = None,
    ) -> tuple[list[Hit], list[Hit]]:
        """(nearest by meaning, best by words), one round trip. The word
        search is skipped for a question with no searchable words.
        `conditions` narrow both to chunks with those payload values."""
        flt = {"filter": {"must": _must(conditions)}} if conditions else {}
        searches = [
            {"query": list(dense), "using": DENSE, "limit": pool, "with_payload": True, **flt}
        ]
        if sparse["indices"]:
            searches.append(
                {"query": sparse, "using": KEYWORDS, "limit": pool, "with_payload": True, **flt}
            )
        result = self._call(
            "POST", f"/collections/{self.collection}/points/query/batch", {"searches": searches}
        )
        lists = [
            [Hit(str(p["id"]), p["score"], p["payload"]) for p in group["points"]]
            for group in result
        ]
        return lists[0], (lists[1] if len(lists) > 1 else [])
