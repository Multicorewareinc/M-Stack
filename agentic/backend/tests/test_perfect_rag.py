"""The perfect-RAG stand-in (tests/eval/perfect_rag.py): each case's
request gets exactly its reference passages, in the RAG service's own
format, and nothing else gets anything."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import perfect_rag
from orchestration import grounding
from orchestration.grounding import search_query
from script_cases import SCRIPT_CASES

CASES = {c.id: c for c in SCRIPT_CASES}


@pytest.fixture(scope="module")
def table():
    return perfect_rag.answers()


def test_every_case_with_a_reference_passage_is_answered_with_exactly_those(table):
    with_passage = [c for c in SCRIPT_CASES if c.reference_context_ids]
    assert len(table) == len(with_passage)
    for case in with_passage:
        found = table[search_query(case.request)]
        assert [f"{p.path}:{p.start}-{p.end}" for p in found] == list(case.reference_context_ids)


def test_the_passage_is_rendered_as_the_rag_service_renders_it(table):
    case = CASES["gateway-fail-open"]
    body = perfect_rag.search(search_query(case.request), table)
    assert body["rendered"].startswith(perfect_rag.prompt.INSTRUCTIONS)
    assert "### [1] multistack/gateway/spec.py:134-136\n" in body["rendered"]
    assert "policy_fail_mode" in body["rendered"]
    assert body["passages"][0]["path"] == "multistack/gateway/spec.py"


def test_a_case_without_a_reference_passage_gets_nothing(table):
    body = perfect_rag.search(search_query(CASES["chain-gateway"].request), table)
    assert body["passages"] == [] and body["rendered"] == ""


def test_the_agent_gets_the_passage_through_its_own_retrieval(table, monkeypatch):
    httpd = perfect_rag.server(0, table)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.setenv("RAG_SERVICE_URL", f"http://127.0.0.1:{httpd.server_port}")
    grounding.reset()
    try:
        rendered, sources = grounding.retrieve(CASES["gateway-fail-open"].request, ["gateway"])
        rejection = grounding.retrieve_for_rejection("gateway.policy_fail_mode: invalid", [])
    finally:
        httpd.shutdown()
        grounding.reset()
    assert [(s["path"], s["start"], s["end"]) for s in sources] == [
        ("multistack/gateway/spec.py", 134, 136)
    ]
    assert "policy_fail_mode" in rendered
    # A rejection lookup is not a case's request: perfect RAG adds nothing.
    assert rejection == ""
