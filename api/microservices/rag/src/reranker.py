"""The reranking model: scores how well each candidate answers the question.

Hybrid search ranks the question against each chunk separately: by the
distance between two embeddings, and by shared words. A reranker (a
cross-encoder) reads the question and the chunk together, so it can tell a
chunk that answers the question from one that only mentions the same
things. It is too slow to run over the whole index, so it only reorders the
top of the hybrid ranking.

Calls the Cohere/Jina-style `/v1/rerank` API that vLLM serves for
cross-encoder models. Some rerankers expect the question and each document
wrapped in a prompt (Qwen3-Reranker does); the two templates are settings,
and plain `{query}` / `{document}` send them as they are.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx


class RerankError(RuntimeError):
    """The reranker could not be reached, or answered with something that
    can't be trusted to reorder the candidates."""


@dataclass(frozen=True)
class RerankConfig:
    url: str
    model: str
    timeout_seconds: float = 3.0
    query_template: str = "{query}"
    document_template: str = "{document}"
    # A long function would dominate the request; its opening says what
    # it is about, which is what the reranker needs.
    max_document_chars: int = 2000


class Reranker:
    def __init__(self, config: RerankConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        """One relevance score per document, in the order given."""
        if not documents:
            return []
        c = self.config
        body = {
            "model": c.model,
            "query": c.query_template.format(query=query),
            "documents": [
                c.document_template.format(document=d[: c.max_document_chars]) for d in documents
            ],
        }
        try:
            response = self._client.post(c.url, json=body)
        except httpx.HTTPError as e:
            raise RerankError(f"could not reach the reranker at {c.url}: {e}") from e
        if response.status_code != 200:
            raise RerankError(
                f"the reranker answered {response.status_code}: {response.text[:300]}"
            )
        try:
            results = response.json()["results"]
            scores = {int(r["index"]): float(r["relevance_score"]) for r in results}
        except (ValueError, KeyError, TypeError) as e:
            raise RerankError(f"the reranker's reply could not be read: {e}") from e
        if sorted(scores) != list(range(len(documents))):
            raise RerankError(
                f"the reranker scored {len(scores)} of {len(documents)} documents, "
                "or indices that were not sent"
            )
        return [scores[i] for i in range(len(documents))]
