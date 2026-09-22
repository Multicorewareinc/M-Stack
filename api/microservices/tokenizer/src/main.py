"""App entrypoint. Builds the FastAPI app, wires settings + service + routes.

Run: uvicorn main:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from fastapi import FastAPI
from router import router
from service import TOKENIZERS, TokenizerService

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


def create_app(settings: Settings | None = None, *, tokenizers: dict | None = None) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    # Lazy import so offline tests (which inject a fake registry) never need tiktoken.
    registry = tokenizers if tokenizers is not None else TOKENIZERS

    # Fail fast at startup, not on every request, if TOKENIZER_DEFAULT is misconfigured.
    if settings.tokenizer_default not in registry:
        raise RuntimeError(
            f"TOKENIZER_DEFAULT={settings.tokenizer_default!r} is not a registered "
            f"tokenizer (registered: {sorted(registry)})"
        )

    service = TokenizerService(tokenizers=registry, default=settings.tokenizer_default)

    app = FastAPI(title="Tokenizer", version="0.1.0")
    app.state.settings = settings
    app.state.service = service

    install_error_handlers(app)
    app.include_router(router)
    return app
