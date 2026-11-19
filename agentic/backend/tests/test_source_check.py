"""The values a valid plan chose, checked against the services that will
run them: which values are looked up, what counts as evidence, and what
the model is told."""

from __future__ import annotations

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from orchestration import graph, grounding, source_check
from orchestration.source_check import chosen_values, message, verify
from test_grounding import PLAN, use_service

from mcp_tools.full_stack import build_full_stack_plan, prepare_args
from mcp_tools.policy import build_policy_plan
from mcp_tools.tokenizer import build_tokenizer_plan


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("RAG_ENABLED", raising=False)
    monkeypatch.setattr(grounding, "_client", None)
    grounding.reset()
    yield
    grounding.reset()


# ---- which values are looked up


def test_a_field_set_away_from_its_default_is_looked_up():
    args = prepare_args({**PLAN, "gateway": {**PLAN["gateway"], "policy_fail_mode": "open"}})
    assert chosen_values(build_full_stack_plan, "build_full_stack_plan", args) == [
        ("gateway", "policy_fail_mode", "open")
    ]


def test_the_users_own_facts_wired_fields_and_defaults_are_not():
    # PLAN is names, Secrets, a URL, a password and wired placeholders,
    # every one either required, a credential, a label or left at default.
    args = prepare_args({**PLAN, "gateway": {**PLAN["gateway"], "policy_fail_mode": "closed"}})
    assert chosen_values(build_full_stack_plan, "build_full_stack_plan", args) == []


def test_options_are_read_as_the_type_chooses_them():
    args = {"tokenizer": {"kubeconfig_path": "/k", "options": {"default_encoding": "o200k_base"}}}
    assert chosen_values(build_tokenizer_plan, "build_tokenizer_plan", args) == [
        ("tokenizer", "default_encoding", "o200k_base")
    ]


def test_a_credential_with_a_default_is_never_sent():
    args = {
        "policy": {
            "kubeconfig_path": "/k",
            "cache_url": "redis://v:6379/0",
            "cache_auth_url": "redis://:s3cret@v:6379/0",
            "limits": {"user_default": 60},
        }
    }
    found = chosen_values(build_policy_plan, "build_policy_plan", args)
    assert ("policy", "user_default", "60") in found
    assert not any("s3cret" in value for _, _, value in found)


# ---- what counts as evidence

USE = {
    "capability": "tokenizer",
    "name": "default_encoding",
    "value": "o200k_base",
    "passages": [
        {
            "path": "api/microservices/tokenizer/src/service.py",
            "start": 23,
            "end": 25,
            "context": "constant TOKENIZERS",
            "text": 'TOKENIZERS: dict = {\n    "cl100k_base": _encode_cl100k_base,\n}',
        }
    ],
}


def answering(text):
    asked = []

    def ask(messages):
        asked.append(messages)
        return text

    return ask, asked


def test_a_value_the_source_rules_out_is_reported_with_its_line():
    ask, asked = answering(
        '[{"setting": 1, "quote": "\\"cl100k_base\\": _encode_cl100k_base,", '
        '"supported": ["cl100k_base"]}]'
    )
    [flag] = verify([USE], ask)
    assert flag["where"] == "api/microservices/tokenizer/src/service.py:23-25"
    assert flag["supported"] == ["cl100k_base"]
    assert "o200k_base" in asked[0][1].content and "TOKENIZERS" in asked[0][1].content


def test_a_multi_line_definition_quoted_on_one_line_is_still_evidence():
    # Seen live: the registry quoted whole, its three lines joined.
    ask, _ = answering(
        '[{"setting": 1, "quote": "TOKENIZERS: dict = {\\"cl100k_base\\": _encode_cl100k_base,}"}]'
    )
    [flag] = verify([USE], ask)
    assert flag["where"].endswith(":23-25")


def test_a_quote_that_isnt_in_the_source_is_not_evidence():
    ask, _ = answering('[{"setting": 1, "quote": "ONLY = [\\"cl100k_base\\"]"}]')
    assert verify([USE], ask) == []


@pytest.mark.parametrize("answer", ["[]", "no", '[{"setting": 7, "quote": "x"}]', ""])
def test_nothing_reported_unreadably_or_out_of_range_flags_nothing(answer):
    ask, _ = answering(answer)
    assert verify([USE], ask) == []


def test_a_failing_model_flags_nothing():
    def ask(messages):
        raise TimeoutError("slow")

    assert verify([USE], ask) == []


def test_settings_with_no_service_code_are_not_asked_about():
    ask, asked = answering("[]")
    assert verify([{**USE, "passages": []}], ask) == [] and asked == []


def test_the_model_is_told_the_line_and_to_ask():
    text = message(
        [{**USE, "quote": '"cl100k_base": _encode_cl100k_base,', "where": "s.py:23-25",
          "supported": ["cl100k_base"]}]
    )
    assert "default_encoding='o200k_base'" in text and "s.py:23-25" in text
    assert "cl100k_base" in text and "ask" in text


# ---- the service calls


def test_field_docs_asks_for_the_capabilities_in_order(monkeypatch):
    reply = {"passages": [{**USE["passages"][0], "kind": "field", "capability": "tokenizer",
                           "score": 0.0, "rerank_score": None}], "rendered": "Reference ..."}
    seen = use_service(monkeypatch, lambda r: httpx.Response(200, json=reply))
    text, sources = grounding.field_docs(["tokenizer", "enricher"])
    assert json.loads(seen[0].content) == {"capabilities": ["tokenizer", "enricher"]}
    assert seen[0].url.path == "/v1/fields"
    assert text == "Reference ..." and "text" not in sources[0]


def test_value_uses_sends_each_value(monkeypatch):
    seen = use_service(monkeypatch, lambda r: httpx.Response(200, json={"uses": [USE]}))
    assert grounding.value_uses([("tokenizer", "default_encoding", "o200k_base")]) == [USE]
    assert json.loads(seen[0].content)["items"] == [
        {"capability": "tokenizer", "name": "default_encoding", "value": "o200k_base"}
    ]


def test_a_failing_service_gives_nothing(monkeypatch):
    use_service(monkeypatch, lambda r: httpx.Response(503, json={}))
    assert grounding.field_docs(["tokenizer"]) == ("", [])
    assert grounding.value_uses([("tokenizer", "default_encoding", "x")]) == []


# ---- in the graph


def _run(args, monkeypatch):
    call = {"name": "build_full_stack_plan", "args": args, "id": "c1"}
    state = {
        "messages": [HumanMessage(content="add a gateway"), AIMessage(content="", tool_calls=[call])],
        "skills": ["gateway"],
        "script": None,
        "attempt": 1,
    }
    update = graph.execute_tools(state)
    return update, json.loads(update["messages"][0].content)


def test_a_valid_plan_with_a_value_its_service_rejects_goes_back_to_the_model(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "1")
    gateway_use = {**USE, "capability": "gateway", "name": "policy_fail_mode", "value": "sideways",
                   "passages": [{**USE["passages"][0], "text": 'MODES = ("open", "closed")'}]}
    monkeypatch.setattr(source_check, "value_uses", lambda items: [gateway_use])
    monkeypatch.setattr(
        graph, "_ask_unbound", lambda messages: '[{"setting": 1, "quote": "MODES = (\\"open\\", \\"closed\\")"}]'
    )
    args = {**PLAN, "gateway": {**PLAN["gateway"], "policy_fail_mode": "open"}}
    update, result = _run(args, monkeypatch)
    assert result["valid"] is False and "MODES" in result["error"]
    # Explicitly cleared, not just absent: a rejected call must not leave
    # an earlier attempt's script looking like this turn's real answer
    # (see execute_tools in graph.py).
    assert update.get("script") is None
    assert result["checked"][0]["path"] == "api/microservices/tokenizer/src/service.py"


def test_a_valid_plan_whose_values_check_out_stays_valid(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.setattr(source_check, "value_uses", lambda items: [USE])
    monkeypatch.setattr(graph, "_ask_unbound", lambda messages: "[]")
    update, result = _run(PLAN, monkeypatch)
    assert result["valid"] is True and update["script"]


def test_with_retrieval_off_nothing_is_checked(monkeypatch):
    monkeypatch.setattr(
        source_check, "value_uses", lambda items: pytest.fail("no check without retrieval")
    )
    update, result = _run(PLAN, monkeypatch)
    assert result["valid"] is True and "checked" not in result
