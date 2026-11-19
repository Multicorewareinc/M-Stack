"""Conversations kept on disk (AGENTIC_STATE_DB): what a restart of the
agent keeps. A restart is modelled as the process would see it -- the
compiled graph thrown away and built again over the same file."""

from __future__ import annotations

import itertools
import uuid

import pytest
from langchain_core.messages import AIMessage, SystemMessage

import orchestration.graph as graph
from orchestration import needs

_ids = itertools.count()


class ScriptedModel:
    def __init__(self, replies: list[AIMessage]) -> None:
        self._replies = iter(replies)

    def invoke(self, _messages):
        return next(self._replies)

    async def ainvoke(self, _messages):
        # call_model awaits `ainvoke`, not `invoke` (see orchestration/graph.py).
        return next(self._replies)


def _tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"disk-{next(_ids)}"}])


@pytest.fixture
def on_disk(monkeypatch, tmp_path):
    """send(thread, message, replies) against a graph whose conversations
    are in a file, and restart() to rebuild the graph over that file."""
    monkeypatch.setenv("AGENTIC_STATE_DB", str(tmp_path / "nested" / "conversations.db"))
    monkeypatch.setattr(graph, "_ask_unbound", lambda messages: "no")
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    monkeypatch.setattr(graph, "_GRAPH", None)

    async def send(thread: str, message: str, replies: list[AIMessage]) -> dict:
        model = ScriptedModel(replies)
        monkeypatch.setattr(graph, "_model", lambda _tools=(): model)
        return await graph.send_message(thread, message)

    def restart() -> None:
        monkeypatch.setattr(graph, "_GRAPH", None)

    yield send, restart
    monkeypatch.setattr(graph, "_GRAPH", None)


async def test_a_question_waiting_for_its_answer_survives_a_restart(on_disk):
    send, restart = on_disk
    thread = uuid.uuid4().hex
    paused = await send(thread, "a valkey cache called sessions", [_tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})])
    assert paused["ask_user"]["options"] == [needs.NEW_CLUSTER, needs.EXISTING_CLUSTER]

    restart()
    assert (await graph.aget_state(thread))["ask_user"]["options"] == [needs.NEW_CLUSTER, needs.EXISTING_CLUSTER]
    resumed = await send(thread, needs.EXISTING_CLUSTER, [AIMessage(content="Where is its kubeconfig file?")])
    assert resumed["reply"] == "Where is its kubeconfig file?"


async def test_a_finished_turn_survives_a_restart_and_the_next_message_continues_it(on_disk):
    send, restart = on_disk
    thread = uuid.uuid4().hex
    args = {"valkey": {"kubeconfig_path": "/etc/rancher/rke2/rke2.yaml", "name": "sessions"}}
    built = await send(
        thread, "a valkey cache called sessions. kubeconfig is /etc/rancher/rke2/rke2.yaml",
        [_tool_call("build_valkey_plan", args), AIMessage(content="Here is the plan.")],
    )
    assert built["script"]

    restart()
    kept = await graph.aget_state(thread)
    assert kept["script"] == built["script"] and kept["skills"] == built["skills"]
    after = await send(thread, "what namespace does it use?", [AIMessage(content="The valkey namespace.")])
    assert after["reply"] == "The valkey namespace."
    # Continued, not started over: the base instructions were seeded once.
    systems = [m for m in after["messages"] if isinstance(m, SystemMessage)]
    assert sum("SCOPE:" in str(m.content) for m in systems) == 1


def test_without_the_setting_conversations_are_in_memory(monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver

    monkeypatch.delenv("AGENTIC_STATE_DB", raising=False)
    assert isinstance(graph._checkpointer(), MemorySaver)
