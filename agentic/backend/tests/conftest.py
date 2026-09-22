"""Test helpers: build the app with the orchestration graph replaced by an
in-memory fake, so tests never invoke a real LLM, MCP tool, or piece of
infrastructure. Swap FakeGraph's behavior for whatever a given test needs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import create_app
from settings import Settings


class FakeGraph:
    """Stands in for orchestration/graph.py's module-level API
    (send_message/get_state). `states` seeds what get_state() reports for
    a given thread_id; send_message() just records the call and echoes a
    fixed reply unless a per-thread response was queued via `responses`."""

    def __init__(
        self,
        *,
        responses: dict[str, dict] | None = None,
        states: dict[str, dict] | None = None,
        events: list[dict] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._states = states or {}
        self._events = events or []
        self.calls: list[tuple[str, tuple]] = []

    def send_message(self, thread_id: str, message: str) -> dict:
        self.calls.append(("send_message", (thread_id, message)))
        return self._responses.get(thread_id, {"reply": f"echo: {message}", "script": None})

    def stream_events(self, thread_id: str, message: str):
        """Yields the queued `events`, then the same final state
        send_message() would have returned -- the real stream_events()
        always ends that way."""
        self.calls.append(("stream_events", (thread_id, message)))
        yield from self._events
        yield {"type": "final", **self._responses.get(thread_id, {"reply": f"echo: {message}", "script": None})}

    def get_state(self, thread_id: str) -> dict:
        return self._states.get(thread_id, {})


def make_client(graph=None, **settings_kwargs):
    """Build the app with a FakeGraph (or the one passed in) instead of a
    real orchestration graph. `service_api_key` defaults to a fixed test
    value so callers don't have to pass X-API-Key auth explicitly for it."""
    settings_kwargs.setdefault("service_api_key", "test-key")
    app = create_app(Settings(**settings_kwargs), graph=graph if graph is not None else FakeGraph())
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client_factory():
    return make_client
