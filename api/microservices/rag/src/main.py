"""App entrypoint. Builds the FastAPI app and wires settings, the vector
database, the embedding client and the searcher.

Run: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000

Stateless: everything searched lives in the vector database; the index is
written by indexer.py, not by this service.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from embeddings import Embedder, EmbeddingConfig
from fastapi import FastAPI
from llm_reranker import LLMRerankConfig, LLMReranker
from reranker import RerankConfig, Reranker
from router import router
from service import Searcher
from store import QdrantStore

from errors import install_error_handlers
from settings import Settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app(settings: Settings | None = None, *, searcher: Searcher | None = None) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    if searcher is None:
        store = QdrantStore(
            settings.qdrant_url,
            settings.qdrant_collection,
            api_key=settings.qdrant_api_key,
            timeout=settings.qdrant_timeout_seconds,
        )
        embedder = Embedder(
            EmbeddingConfig(
                url=settings.embedding_url,
                model=settings.embedding_model,
                batch_size=settings.embedding_batch_size,
                timeout_seconds=settings.search_embedding_timeout_seconds,
                query_instruction=settings.query_instruction,
                expected_dimension=settings.embedding_dimension,
                query_template=settings.query_template,
                document_template=settings.document_template,
                max_input_tokens=settings.embedding_max_input_tokens,
            )
        )
        reranker = None
        if settings.rerank_url:
            reranker = Reranker(
                RerankConfig(
                    url=settings.rerank_url,
                    model=settings.rerank_model,
                    timeout_seconds=settings.rerank_timeout_seconds,
                    query_template=settings.rerank_query_template,
                    document_template=settings.rerank_document_template,
                )
            )
        llm_reranker = None
        if settings.llm_rerank_url:
            llm_reranker = LLMReranker(
                LLMRerankConfig(
                    url=settings.llm_rerank_url,
                    model=settings.llm_rerank_model,
                    timeout_seconds=settings.llm_rerank_timeout_seconds,
                    subject=settings.llm_rerank_subject,
                )
            )
        searcher = Searcher(store, embedder, settings, reranker, llm_reranker)

    app = FastAPI(title="RAG Reference Search", version="0.1.0")
    app.state.settings = settings
    app.state.searcher = searcher
    install_error_handlers(app)
    app.include_router(router)
    return app
