"""The agent's own checks on the model's turn: a reply asking the user for
what the plan doesn't need from them goes back to the model instead of
the user, and a plan that builds a new cluster beside the one the user
has is refused with the reason."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from orchestration import graph


@pytest.fixture(autouse=True)
def no_scope_check(monkeypatch):
    # scope_check.enforce calls the real judge model -- every test here
    # exercises execute_tools' plain-text-reply path directly, with no
    # tool call, so it's the one file that hits that call unmocked.
    # scope_check's own behaviour is tested in test_scope_check.py.
    monkeypatch.setattr(graph.scope_check, "enforce", lambda reply, ask, **_: reply)

EXISTING = (
    "Add a model gateway. The cluster's kubeconfig is /etc/rancher/rke2/rke2.yaml and our "
    "models are served by vLLM at http://vllm.ai.svc:8000/v1."
)
NEW = "Build a new RKE2 cluster called edge."


def _reply(request: str, text: str, *, attempt: int = 1, script: str | None = None) -> dict:
    state = {
        "messages": [HumanMessage(content=request), AIMessage(content=text)],
        "attempt": attempt,
        "script": script,
        "skills": [],
    }
    return state, graph.execute_tools(state)


def _held_back(update: dict) -> bool:
    return update["reply"] is None and any(
        isinstance(m, HumanMessage) and m.content.startswith("[Agent check")
        for m in update.get("messages", [])
    )


def test_a_reply_asking_for_a_wired_field_goes_back_to_the_model():
    state, update = _reply(EXISTING, "Please provide the gateway's `org_cp_internal_url`.")
    assert _held_back(update)
    assert "org_cp_internal_url" in update["messages"][0].content
    assert graph.route_after_tools({**state, **update}) == "continue"


def test_a_reply_naming_a_wired_field_in_plain_words_also_goes_back():
    # The model rarely names the field literally -- it paraphrases it, as
    # seen in a real evaluation run (chain-tokenizer, RAG on).
    state, update = _reply(
        EXISTING,
        "Please provide the organization control plane's internal URL "
        "(you've given the secret name org-cp-secrets, but the URL itself is still needed).",
    )
    assert _held_back(update)
    assert graph.route_after_tools({**state, **update}) == "continue"


def test_asking_for_what_only_the_user_knows_reaches_the_user():
    _, update = _reply(EXISTING, "What password should the appdb database use?")
    assert update["reply"].startswith("What password")


def test_asking_for_the_nodes_of_a_cluster_the_user_wants_built_reaches_the_user():
    _, update = _reply(NEW, "Which addresses do the cluster nodes have?")
    assert update["reply"].startswith("Which addresses")


def test_once_a_plan_is_built_a_reply_naming_wired_fields_goes_out():
    _, update = _reply(EXISTING, "Done: org_cp_internal_url is wired for you.", script="x = 1")
    assert update["reply"].startswith("Done")


def test_on_the_last_turn_the_reply_goes_out_whatever_it_asks():
    _, update = _reply(
        EXISTING, "Please provide org_cp_internal_url.", attempt=graph.MAX_MODEL_TURNS
    )
    assert update["reply"].startswith("Please provide")


def _plan(request: str, args: dict, monkeypatch) -> dict:
    monkeypatch.setattr(graph, "retrieve_for_rejection", lambda error, skills: "")
    call = {"name": "build_full_stack_plan", "args": args, "id": "c1"}
    state = {
        "messages": [HumanMessage(content=request), AIMessage(content="", tool_calls=[call])],
        "attempt": 1,
        "script": None,
        "skills": [],
    }
    return json.loads(graph.execute_tools(state)["messages"][0].content)


def test_a_new_cluster_beside_the_users_cluster_is_refused(monkeypatch):
    result = _plan(
        EXISTING, {"cluster": {"name": "x"}, "storage": {"type": "longhorn"}}, monkeypatch
    )
    assert result["valid"] is False
    assert "already exists" in result["error"] and "cluster and storage" in result["error"]


def test_a_new_cluster_the_user_asked_for_is_not_refused_for_it(monkeypatch):
    result = _plan(NEW, {"cluster": {"name": "edge"}}, monkeypatch)
    assert "already exists" not in result.get("error", "")
