"""Turns text into vectors by calling an OpenAI-compatible embedding server.

Two entry points, because retrieval models are trained asymmetrically:
a stored chunk and a search question are each wrapped the way the model
expects. Each model has its own wrapping, so both are templates:

    model            QUERY_TEMPLATE                            DOCUMENT_TEMPLATE
    Qwen3-Embedding  Instruct: {instruction}\\nQuery:{query}    {text}
    E5               query: {query}                            passage: {text}
    BGE (v1.5)       Represent this sentence for searching     {text}
                     relevant passages: {query}

A text longer than the model accepts is cut to MAX_INPUT_TOKENS rather
than refused by the server.

Batches go out one at a time, never in parallel: the server is shared
with the chat model.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx
from chunking import Chunk

logger = logging.getLogger(__name__)

DEFAULT_QUERY_INSTRUCTION = (
    "Given a question about deploying Multistack infrastructure, "
    "retrieve the SDK fields, rules and code that answer it"
)

DEFAULT_QUERY_TEMPLATE = "Instruct: {instruction}\nQuery:{query}"
DEFAULT_DOCUMENT_TEMPLATE = "{text}"

# A collection's vector width is fixed when it is created, so a model swap
# that changes this has to fail here rather than at insert time.
EXPECTED_DIMENSION = 1024


class EmbeddingError(RuntimeError):
    """The embedding server could not be reached, or answered with
    something that can't safely be stored."""


@dataclass(frozen=True)
class EmbeddingConfig:
    url: str
    model: str
    batch_size: int = 32
    timeout_seconds: float = 60.0
    query_instruction: str = DEFAULT_QUERY_INSTRUCTION
    expected_dimension: int = EXPECTED_DIMENSION
    query_template: str = DEFAULT_QUERY_TEMPLATE
    document_template: str = DEFAULT_DOCUMENT_TEMPLATE
    # The model's input limit; three characters per token is a pessimistic
    # estimate for code.
    max_input_tokens: int = 4096

    def query_text(self, question: str) -> str:
        return self._cut(
            self.query_template.replace("{instruction}", self.query_instruction).replace(
                "{query}", question
            )
        )

    def document_text(self, text: str) -> str:
        return self._cut(self.document_template.replace("{text}", text))

    def _cut(self, text: str) -> str:
        return text[: self.max_input_tokens * 3]

    @classmethod
    def from_env(cls) -> EmbeddingConfig:
        url = os.environ.get("EMBEDDING_URL", "").strip()
        model = os.environ.get("EMBEDDING_MODEL", "").strip()
        missing = [
            name
            for name, value in (("EMBEDDING_URL", url), ("EMBEDDING_MODEL", model))
            if not value
        ]
        if missing:
            raise EmbeddingError(f"{' and '.join(missing)} must be set (see .env.example)")
        return cls(
            url=url,
            model=model,
            batch_size=int(os.environ.get("EMBEDDING_BATCH_SIZE", cls.batch_size)),
            query_instruction=os.environ.get("QUERY_INSTRUCTION", DEFAULT_QUERY_INSTRUCTION),
            query_template=os.environ.get("QUERY_TEMPLATE", DEFAULT_QUERY_TEMPLATE),
            document_template=os.environ.get("DOCUMENT_TEMPLATE", DEFAULT_DOCUMENT_TEMPLATE),
            max_input_tokens=int(
                os.environ.get("EMBEDDING_MAX_INPUT_TOKENS", cls.max_input_tokens)
            ),
        )


def _normalise(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise EmbeddingError("the server returned an all-zero vector")
    return [x / norm for x in vector]


class Embedder:
    def __init__(
        self, config: EmbeddingConfig | None = None, *, client: httpx.Client | None = None
    ) -> None:
        self.config = config or EmbeddingConfig.from_env()
        self._client = client or httpx.Client(timeout=self.config.timeout_seconds)

    def embed_documents(
        self,
        chunks: Sequence[Chunk],
        *,
        on_batch: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        """One vector per chunk, in the same order. `on_batch(done, total)`
        is called after each batch so a long run can show progress."""
        texts = [self.config.document_text(c.embed_text) for c in chunks]
        return self._embed(texts, on_batch=on_batch)

    def embed_query(self, question: str) -> list[float]:
        return self._embed([self.config.query_text(question)])[0]

    def embed_queries(self, questions: Sequence[str]) -> list[list[float]]:
        """One vector per question, in as few calls as the batch size allows."""
        return self._embed([self.config.query_text(q) for q in questions])

    def _embed(
        self, texts: Sequence[str], *, on_batch: Callable[[int, int], None] | None = None
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        size = self.config.batch_size
        for start in range(0, len(texts), size):
            vectors.extend(self._embed_batch(texts[start : start + size]))
            logger.info("embedded %d/%d", len(vectors), len(texts))
            if on_batch:
                on_batch(len(vectors), len(texts))
        return vectors

    def _embed_batch(self, batch: Sequence[str]) -> list[list[float]]:
        try:
            response = self._client.post(
                self.config.url, json={"model": self.config.model, "input": list(batch)}
            )
        except httpx.HTTPError as e:
            raise EmbeddingError(
                f"could not reach the embedding server at {self.config.url}: {e}"
            ) from e

        if response.status_code != 200:
            raise EmbeddingError(
                f"embedding server returned {response.status_code}: {response.text[:300]}"
            )

        try:
            items = response.json()["data"]
        except (ValueError, KeyError, TypeError) as e:
            raise EmbeddingError(
                f"embedding server sent an unreadable response: {response.text[:300]}"
            ) from e

        # A short or long answer would silently pair chunks with the wrong
        # vectors from here on, so the count is checked, not assumed.
        if len(items) != len(batch):
            raise EmbeddingError(f"sent {len(batch)} texts, got {len(items)} vectors back")

        ordered = sorted(items, key=lambda item: item["index"])
        vectors = []
        for item in ordered:
            vector = item["embedding"]
            if len(vector) != self.config.expected_dimension:
                raise EmbeddingError(
                    f"expected {self.config.expected_dimension}-dimension vectors, got {len(vector)}: "
                    f"is the server running a different model than {self.config.model!r}?"
                )
            vectors.append(_normalise(vector))
        return vectors


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product; equal to cosine similarity because every vector this
    module returns has unit length."""
    return sum(x * y for x, y in zip(a, b, strict=True))
