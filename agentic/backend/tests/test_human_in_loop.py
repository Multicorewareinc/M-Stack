"""Tests for human-in-the-loop: orchestration/graph.py's confirm_selection
node (paused on a genuinely borderline skill match) and the ask_user tool
(paused on the model's own judgment that a request admits two valid
approaches) -- both built on LangGraph's interrupt()/Command(resume=...),
both resumed the same way a thread is always continued: send_message()
with the human's answer as the next message.

Run against the real graph, like test_streaming.py's own turn-level
tests -- only the chat model is scripted, so these exercise LangGraph's
actual pause/resume machinery, not a stand-in for it.
"""

from __future__ import annotations

import itertools
import uuid

import pytest
from langchain_core.messages import AIMessage

import orchestration.graph as graph

VALKEY_ARGS = {"valkey": {"kubeconfig_path": "/tmp/kc.yaml", "name": "sessions", "namespace": "demo"}}
_ids = itertools.count()


class ScriptedModel:
    """Replays a fixed list of model replies, one per call_model turn."""

    def __init__(self, replies: list[AIMessage]) -> None:
        self._replies = iter(replies)

    def invoke(self, _messages):
        return next(self._replies)

    async def ainvoke(self, _messages):
        # call_model awaits `ainvoke`, not `invoke` (see orchestration/graph.py).
        return next(self._replies)


def _tool_call(name: str, args: dict, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args, "id": f"call-{next(_ids)}"}])


@pytest.fixture
def send(monkeypatch):
    """send(thread_id, message, replies=[...]) -- sends one message on a
    thread with the model scripted, every judgement call (_ask_unbound)
    stubbed to "no" (see test_streaming.py's identical fixture for why),
    and selection_ambiguity() left at the real function unless a test
    overrides it -- RAG is off in this test environment, so it returns
    None (no ambiguity) by default, same as no RAG availability always
    has meant for every RAG-backed path in this codebase."""
    monkeypatch.setattr(graph, "_ask_unbound", lambda messages: "no")

    async def _send(thread_id: str, message: str, replies: list[AIMessage] | None = None) -> dict:
        if replies is not None:
            model = ScriptedModel(replies)
            monkeypatch.setattr(graph, "_model", lambda _tools=(): model)
        return await graph.send_message(thread_id, message)

    return _send


# ---- confirm_selection: paused on a genuinely borderline skill match


async def test_confirm_selection_pauses_when_selection_is_ambiguous(send, monkeypatch):
    monkeypatch.setattr(
        graph, "selection_ambiguity",
        lambda text: {"top_skill": "policy", "margin": 0.61, "runner_up": "gateway", "runner_up_margin": 0.60},
    )
    thread_id = uuid.uuid4().hex
    result = await send(thread_id, "add something rate-limit related")

    assert "ask_user" in result
    assert result["ask_user"]["question"]
    assert result["ask_user"]["options"] == ["policy", "gateway"]
    # Paused inside confirm_selection itself, before the model ever ran --
    # it's the entry point, ahead of call_model.
    snapshot = await graph._asnapshot(thread_id)
    assert snapshot.next == ("confirm_selection",)


async def test_confirm_selection_does_not_pause_when_selection_is_clear(send, monkeypatch):
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    thread_id = uuid.uuid4().hex
    result = await send(
        thread_id, "a valkey cache called sessions",
        replies=[_tool_call("build_valkey_plan", VALKEY_ARGS), AIMessage(content="Here is the plan.")],
    )

    assert "ask_user" not in result
    assert (await graph._asnapshot(thread_id)).interrupts == ()


async def test_resuming_a_confirm_selection_pause_continues_the_same_turn(send, monkeypatch):
    monkeypatch.setattr(
        graph, "selection_ambiguity",
        lambda text: {"top_skill": "policy", "margin": 0.61, "runner_up": None, "runner_up_margin": None},
    )
    thread_id = uuid.uuid4().hex
    paused = await send(thread_id, "add something rate-limit related")
    assert "ask_user" in paused

    # The model only ever sees this reply once resumed -- scripting it
    # here, not above, proves confirm_selection's pause genuinely
    # happened before call_model ran at all.
    resumed = await send(thread_id, "no, leave policy out", replies=[AIMessage(content="Understood, skipping that.")])
    assert "ask_user" not in resumed
    assert resumed["reply"] == "Understood, skipping that."
    # The human's answer reached the model as a real message, not a side
    # channel it never saw.
    texts = [str(m.content) for m in resumed["messages"]]
    assert any("no, leave policy out" in t for t in texts)


# ---- ask_user: the model's own judgment that a request is genuinely ambiguous
#
# A choice the model offers before any build tool has been tried is held
# back once (the tool's own findings are asked first); offered again, it
# pauses the turn. These tests script the model asking twice.


async def test_ask_user_tool_call_pauses_the_turn(send, monkeypatch):
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    thread_id = uuid.uuid4().hex
    result = await send(
        thread_id, "set up a cluster for this",
        replies=[
            _tool_call("ask_user", {
                "question": "Is this a new cluster, or do you already have one?",
                "options": ["new cluster", "existing cluster"],
            })
            for _ in range(2)
        ],
    )

    # This is the real regression guard: without re-raising GraphBubbleUp
    # ahead of execute_tools()'s broad `except Exception`, this would
    # come back as a fake tool failure instead of an actual pause.
    assert "ask_user" in result
    assert result["ask_user"]["question"] == "Is this a new cluster, or do you already have one?"
    assert result["ask_user"]["options"] == ["new cluster", "existing cluster"]
    assert "error" not in result  # confirms it did NOT get caught as a tool failure


async def test_resuming_an_ask_user_pause_feeds_the_answer_back_as_the_tools_result(send, monkeypatch):
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    thread_id = uuid.uuid4().hex
    await send(
        thread_id, "set up a cluster for this",
        replies=[
            _tool_call("ask_user", {"question": "New or existing?", "options": ["new", "existing"]})
            for _ in range(2)
        ],
    )

    resumed = await send(
        thread_id, "existing",
        replies=[AIMessage(content="Got it, building onto your existing cluster.")],
    )
    assert "ask_user" not in resumed
    assert resumed["reply"] == "Got it, building onto your existing cluster."


# ---- cluster fork: new-cluster-or-existing, checked against the tool's
# own real rejection rather than left to the model to raise -- see
# graph._cluster_fork_needed's own docstring for why a prompt instruction
# alone was tried first and measured unreliable.


async def test_cluster_fork_pauses_when_kubeconfig_is_missing_and_unstated(send, monkeypatch):
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    thread_id = uuid.uuid4().hex
    result = await send(
        thread_id, "add a valkey cache called sessions",
        replies=[_tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})],
    )

    assert "ask_user" in result
    assert result["ask_user"]["question"]
    assert result["ask_user"]["options"] == ["Create a new cluster", "Use an existing cluster"]
    assert "error" not in result


async def test_cluster_fork_does_not_pause_when_the_request_already_says_existing(send, monkeypatch):
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    thread_id = uuid.uuid4().hex
    result = await send(
        thread_id, "add a valkey cache called sessions to my existing cluster",
        replies=[
            _tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}}),
            AIMessage(content="What's the kubeconfig_path for that cluster?"),
        ],
    )

    assert "ask_user" not in result
    assert result["reply"] == "What's the kubeconfig_path for that cluster?"


async def test_cluster_fork_does_not_pause_when_a_new_cluster_is_composed_in_the_same_call(send, monkeypatch):
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    thread_id = uuid.uuid4().hex
    result = await send(
        thread_id, "build a new cluster with a valkey cache called sessions",
        replies=[
            _tool_call("build_full_stack_plan", {
                "cluster": {"name": "demo", "nodes": [{"address": "10.0.0.1", "user": "root", "role": "server"}]},
                "valkey": {"name": "sessions"},
            }),
            AIMessage(content="I still need a placeholder kubeconfig_path for the new layers."),
        ],
    )

    # A missing kubeconfig_path here is the forgotten placeholder every
    # composed layer still needs on a new cluster (see
    # mcp_tools/full_stack.py's own SCOPE NOTE), not ambiguity -- `cluster`
    # being composed already answers the fork, so the turn proceeds
    # normally (here, straight through to a built script) instead of
    # pausing on a question that was already answered.
    assert "ask_user" not in result
    assert result["reply"].startswith("I still need a placeholder kubeconfig_path for the new layers.")


async def test_resuming_a_cluster_fork_pause_continues_the_same_turn(send, monkeypatch):
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    thread_id = uuid.uuid4().hex
    paused = await send(
        thread_id, "add a valkey cache called sessions",
        replies=[_tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})],
    )
    assert "ask_user" in paused

    resumed = await send(
        thread_id, "use an existing cluster",
        replies=[AIMessage(content="Sure -- what's its kubeconfig_path?")],
    )
    assert "ask_user" not in resumed
    assert resumed["reply"] == "Sure -- what's its kubeconfig_path?"
    texts = [str(m.content) for m in resumed["messages"]]
    assert any("use an existing cluster" in t for t in texts)


async def test_stream_events_surfaces_ask_user_as_its_own_event_type(monkeypatch):
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    monkeypatch.setattr(graph, "_ask_unbound", lambda messages: "no")
    model = ScriptedModel([
        _tool_call("ask_user", {"question": "New or existing?", "options": ["new", "existing"]})
        for _ in range(2)
    ])
    monkeypatch.setattr(graph, "_model", lambda _tools=(): model)

    events = [event async for event in graph.stream_events(uuid.uuid4().hex, "set up a cluster")]
    # "tool_call" shows the model's own decision to ask, twice: the first
    # is held back, the second is the pause, "ask_user".
    assert [e["type"] for e in events] == ["tool_call", "tool_call", "ask_user"]
    assert events[-1]["question"] == "New or existing?"
    # No "final" -- the turn is paused, not finished (see stream_events()'s
    # own docstring: the two are mutually exclusive for one call).
