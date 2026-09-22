"""The actual work of the service: turning one session's message into a
call against the orchestration graph (orchestration/graph.py).
Framework-agnostic -- no FastAPI imports. The graph is injected so tests
can pass a fake instead of exercising a real LangGraph graph backed by a
real LLM and real MCP tools (see tests/conftest.py).

The real I/O -- LangGraph's checkpointer and the LLM call -- lives in
orchestration/graph.py + llm.py, not here (see docs/architecture-decision.md
for that split). This module is the injectable seam between the router and
that work, so tests never touch a network or a real model.
"""

from __future__ import annotations

from typing import Iterable, Protocol

from errors import NotFoundError
from settings import Settings


class OrchestrationGraph(Protocol):
    """The subset of orchestration/graph.py's module-level API this
    service depends on. Exists so tests/conftest.py can inject a fake
    with the same shape instead of the real module."""

    def send_message(self, thread_id: str, message: str) -> dict: ...
    def get_state(self, thread_id: str) -> dict: ...
    def stream_events(self, thread_id: str, message: str) -> Iterable[dict]: ...


class AgenticService:
    def __init__(self, settings: Settings, *, graph: OrchestrationGraph | None = None) -> None:
        self._s = settings
        if graph is None:
            # Deferred import: a test that injects a fake graph never
            # needs orchestration.graph's real dependencies (langgraph,
            # langchain-groq, mcp) loaded at all.
            import orchestration.graph as graph
        self._graph = graph

    def start_session(self, session_id: str, message: str) -> dict:
        """Starts a new conversation on a fresh session_id."""
        return self._graph.send_message(session_id, message)

    def continue_session(self, session_id: str, message: str) -> dict:
        """Sends another message on an existing conversation."""
        return self._graph.send_message(session_id, message)

    def stream_session(self, session_id: str, message: str) -> Iterable[dict]:
        """Streams one turn's progress events (see
        orchestration/graph.py's stream_events) instead of blocking until
        the whole turn finishes. Same work, same thread, different shape
        of result -- start_session/continue_session both map onto this
        for a caller that wants to show progress, since the graph itself
        makes no distinction between a thread's first message and a
        later one."""
        return self._graph.stream_events(session_id, message)

    def get_session(self, session_id: str) -> dict:
        """Read-only status snapshot -- what state this session is
        currently in, without sending a new message. Raises NotFoundError
        for an unknown session_id."""
        state = self._graph.get_state(session_id)
        if not state.get("messages"):
            raise NotFoundError("unknown session_id")
        return state
