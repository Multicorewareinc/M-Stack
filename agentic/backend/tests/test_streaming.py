"""Tests for the streamed turn: orchestration/graph.py's stream_events()
and the two /stream routes that serve it as NDJSON.

stream_events() runs against the real LangGraph graph and the real MCP
tools, with only the chat model scripted -- the part under test is how
graph updates become UI events, which a stubbed graph would skip
entirely. The routes are tested with the FakeGraph from conftest.py,
like every other HTTP test here."""

from __future__ import annotations

import itertools
import json
import uuid

import pytest
from langchain_core.messages import AIMessage

import orchestration.graph as graph
from conftest import FakeGraph

VALKEY_ARGS = {"valkey": {"kubeconfig_path": "/tmp/kc.yaml", "name": "sessions", "namespace": "demo"}}


class ScriptedModel:
    """Replays a fixed list of model replies, one per call_model turn."""

    def __init__(self, replies: list[AIMessage]) -> None:
        self._replies = iter(replies)

    def invoke(self, _messages):
        return next(self._replies)


_ids = itertools.count()


def _tool_call(name: str, args: dict, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args, "id": f"call-{next(_ids)}"}])


@pytest.fixture
def run(monkeypatch):
    """Runs one streamed turn on a fresh thread with the model scripted to
    reply with `replies`, and returns every event it yielded."""

    def _run(replies: list[AIMessage], message: str = "a valkey cache called sessions") -> list[dict]:
        model = ScriptedModel(replies)
        monkeypatch.setattr(graph, "_model", lambda _tools=(): model)
        return list(graph.stream_events(uuid.uuid4().hex, message))

    return _run


def test_a_tool_call_streams_the_call_its_result_then_final(run):
    events = run([
        _tool_call("build_valkey_plan", VALKEY_ARGS),
        AIMessage(content="Here is your Valkey plan."),
    ])
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "final"]

    call, result, final = events
    assert call["tool"] == "build_valkey_plan"
    assert call["args"] == VALKEY_ARGS
    assert result == {"type": "tool_result", "tool": "build_valkey_plan", "valid": True, "error": None}
    assert final["reply"].startswith("Here is your Valkey plan.")
    assert final["script"] in final["reply"]
    assert "Cache(" in final["script"]


def test_a_rejected_plan_streams_the_sdk_error(run):
    no_kubeconfig = {"valkey": {"kubeconfig_path": "", "name": "sessions"}}
    events = run([
        _tool_call("build_valkey_plan", no_kubeconfig),
        AIMessage(content="Which kubeconfig should I use?"),
    ])
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["valid"] is False
    assert "kubeconfig_path" in result["error"]
    assert events[-1]["type"] == "final"
    assert events[-1]["script"] is None


def test_text_alongside_a_tool_call_streams_as_reasoning(run):
    events = run([
        _tool_call("build_valkey_plan", VALKEY_ARGS, text="Validating the cache first."),
        AIMessage(content="Done."),
    ])
    assert [e["type"] for e in events] == ["tool_call", "reasoning", "tool_result", "final"]
    assert events[1]["text"] == "Validating the cache first."


def test_a_plain_reply_streams_only_final(run):
    """Text with no tool call is the turn's answer, and final already
    carries it -- streaming it as reasoning too would show it twice."""
    events = run([AIMessage(content="What should the cache be called?")], message="set up a cache")
    assert [e["type"] for e in events] == ["final"]
    assert events[0]["reply"] == "What should the cache be called?"


def test_streamed_and_blocking_turns_leave_the_same_state(run, monkeypatch):
    replies = [_tool_call("build_valkey_plan", VALKEY_ARGS), AIMessage(content="Here it is.")]
    streamed = run(list(replies))[-1]

    model = ScriptedModel(list(replies))
    monkeypatch.setattr(graph, "_model", lambda _tools=(): model)
    blocking = graph.send_message(uuid.uuid4().hex, "a valkey cache called sessions")

    assert streamed["reply"] == blocking["reply"]
    assert streamed["script"] == blocking["script"]


# ---- the HTTP routes

STREAM_ROUTES = ["/v1/sessions/stream", "/v1/sessions/sess-1/messages/stream"]


def _lines(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


@pytest.mark.parametrize("path", STREAM_ROUTES)
def test_stream_routes_require_the_api_key(client_factory, path):
    resp = client_factory().post(path, json={"message": "hi"})
    assert resp.status_code == 401


def test_stream_route_serves_ndjson_ending_in_the_session_response(client_factory):
    events = [
        {"type": "tool_call", "tool": "build_valkey_plan", "args": VALKEY_ARGS},
        {"type": "tool_result", "tool": "build_valkey_plan", "valid": True, "error": None},
    ]
    resp = client_factory(FakeGraph(events=events)).post(
        "/v1/sessions/stream", json={"message": "a cache"}, headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")

    lines = _lines(resp)
    assert lines[:2] == events
    final = lines[-1]
    assert final["type"] == "final"
    assert final["status"] == "needs_input"
    assert final["message"] == "echo: a cache"
    assert final["session_id"]


def test_stream_route_final_is_resolved_once_a_script_exists(client_factory):
    graph_ = FakeGraph(responses={"sess-1": {"reply": "Here you go.", "script": "cache = Cache(...)"}})
    resp = client_factory(graph_).post(
        "/v1/sessions/sess-1/messages/stream", json={"message": "go"}, headers={"X-API-Key": "test-key"},
    )
    final = _lines(resp)[-1]
    assert final == {
        "type": "final",
        "session_id": "sess-1",
        "status": "resolved",
        "message": "Here you go.",
        "script": "cache = Cache(...)",
    }
    assert graph_.calls == [("stream_events", ("sess-1", "go"))]


def test_final_never_carries_the_raw_message_history(client_factory):
    """The graph's state holds LangChain message objects, which are not
    JSON-serialisable -- the route must map it through SessionResponse."""
    graph_ = FakeGraph(responses={"sess-1": {"reply": "ok", "script": None, "messages": [object()]}})
    resp = client_factory(graph_).post(
        "/v1/sessions/sess-1/messages/stream", json={"message": "go"}, headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    assert "messages" not in _lines(resp)[-1]


def test_running_out_of_turns_after_a_valid_script_returns_that_script():
    """The last allowed turn produced a valid plan, and the reply said no
    script had been reached -- telling the user a working plan failed."""
    reply = graph.budget_exhausted({"script": "stack = Stack(kubeconfig_path='/k')\n"})["reply"]
    assert "stack = Stack(kubeconfig_path='/k')" in reply
    assert "didn't reach" not in reply
    assert "didn't reach" in graph.budget_exhausted({"script": None})["reply"]


def test_the_reply_carries_the_validated_script_not_the_models_rewrite(run):
    """Asked to present the script, the model rewrote it -- renamed a
    variable, dropped fields and backend imports. Its code is replaced."""
    rewritten = "Here it is:\n\n```python\nfrom multistack import Cache\nvalkey = Cache(name='x')\n```\n\nCheck the namespace."
    events = run([_tool_call("build_valkey_plan", VALKEY_ARGS), AIMessage(content=rewritten)])
    final = events[-1]
    assert final["script"] in final["reply"]
    assert "valkey = Cache(name='x')" not in final["reply"]
    assert final["reply"].startswith("Here it is:") and "Check the namespace." in final["reply"]


def test_a_reply_with_no_script_is_left_alone(run):
    events = run([AIMessage(content="Which namespace?\n\n```yaml\nexample: 1\n```")], message="set up a cache")
    assert events[-1]["reply"] == "Which namespace?\n\n```yaml\nexample: 1\n```"
