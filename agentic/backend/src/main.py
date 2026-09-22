"""App entrypoint. Builds the FastAPI app, wires settings + service +
routes -- the real "front door" for the agentic pipeline (see
docs/architecture-decision.md's "User/API Layer"). Wraps
orchestration/graph.py's send_message() in a real service instead of the
local REPL in examples/chat.py, which stays a dev/test tool, not the
production interface.

Run:
    cd agentic/backend/src && uvicorn main:create_app --factory --host 0.0.0.0 --port 8000

AUTH
Single shared API key (SERVICE_API_KEY in agentic/backend/.env), checked via the
X-API-Key header (see dependencies.py). Every endpoint requires it except
/health and /metrics -- this service only ever generates and validates a
script, it never provisions anything, but the SDK usage/LLM calls it
triggers still aren't something to expose over HTTP with no auth at all.

KNOWN LIMITATIONS, same as the rest of this layer
- State is in-memory only (LangGraph's MemorySaver). Restarting this
  process loses every session. See docs/architecture-decision.md Phase D.
- No locking around shared state. Concurrent requests against the same
  session_id aren't guarded against a race. Low-risk for a single
  operator today; a real concern once this has multiple simultaneous
  callers.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from fastapi import FastAPI

from errors import install_error_handlers
from router import router
from service import AgenticService, OrchestrationGraph
from settings import Settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
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


def create_app(settings: Settings | None = None, *, graph: OrchestrationGraph | None = None) -> FastAPI:
    settings = settings or Settings()
    _setup_logging(settings.log_level)

    service = AgenticService(settings, graph=graph)

    app = FastAPI(title="MultiStack Agentic Service", version="0.1.0")
    app.state.settings = settings
    app.state.service = service

    install_error_handlers(app)
    app.include_router(router)
    return app
