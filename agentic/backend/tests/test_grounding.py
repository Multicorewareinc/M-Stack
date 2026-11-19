"""Retrieval inside the agent: off by default, fails silent, and only ever
adds text to what the model reads."""

from __future__ import annotations

import json
import subprocess
import uuid

import httpx
import orchestration.graph as graph
import orchestration.grounding as grounding
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

REFERENCE = "Reference material retrieved: replicas defaults to 2."
SOURCES = [
    {
        "path": "a.py",
        "start": 1,
        "end": 2,
        "context": "c",
        "kind": "field",
        "capability": "gateway",
        "score": 0.1,
    }
]


class RecordingModel:
    def __init__(self) -> None:
        self.seen: list[list] = []

    def invoke(self, messages):
        self.seen.append(list(messages))
        return AIMessage(content="ok")

    async def ainvoke(self, messages):
        # call_model awaits `ainvoke`, not `invoke` (see
        # orchestration/graph.py) -- mirrors this class's own `invoke`.
        self.seen.append(list(messages))
        return AIMessage(content="ok")


@pytest.fixture
def model(monkeypatch):
    m = RecordingModel()
    monkeypatch.setattr(graph, "_model", lambda _tools=(): m)
    # The judge model (scope_check, source_check, request_check) is a
    # separate instance from the conversational one mocked above -- left
    # real, a plain-text reply's scope_check call hits the real model.
    monkeypatch.setattr(graph, "_ask_unbound", lambda messages: "no")
    return m


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("RAG_ENABLED", raising=False)
    monkeypatch.setattr(grounding, "_client", None)
    grounding.reset()
    yield
    grounding.reset()


def _user_text(messages) -> str:
    return next(m.content for m in reversed(messages) if isinstance(m, HumanMessage))


# ---- the switch


def test_nothing_is_retrieved_unless_enabled(monkeypatch):
    monkeypatch.setattr(
        grounding, "_get_client", lambda: pytest.fail("the service must not be called")
    )
    assert grounding.retrieve("what is the gateway replicas default", ["gateway"]) == ("", [])


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_enabled_reads_the_usual_spellings(monkeypatch, value):
    monkeypatch.setenv("RAG_ENABLED", value)
    assert grounding.enabled()


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_other_values_leave_it_off(monkeypatch, value):
    monkeypatch.setenv("RAG_ENABLED", value)
    assert not grounding.enabled()


# ---- talking to the service

SERVICE_REPLY = {
    "passages": [
        {
            "path": "multistack/gateway/spec.py",
            "start": 140,
            "end": 140,
            "context": "field 'replicas'",
            "kind": "field",
            "capability": "gateway",
            "score": 0.03,
            "text": "replicas: int = 2",
        }
    ],
    "rendered": "Reference material ... replicas: int = 2",
    "best_similarity": 0.8,
    "skipped": None,
}


def use_service(monkeypatch, handler):
    """Points retrieval at a fake service answering with `handler`, and
    returns the requests it received."""
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.setenv("RAG_SERVICE_URL", "http://rag:8000")
    monkeypatch.setenv("RAG_SERVICE_API_KEY", "k")
    monkeypatch.setattr(grounding, "_index_checked", True)
    monkeypatch.setattr(
        grounding,
        "_client",
        httpx.Client(
            base_url="http://rag:8000",
            headers={"Authorization": "Bearer k"},
            transport=httpx.MockTransport(record),
        ),
    )
    return seen


def test_the_service_is_asked_with_the_message_and_skills_and_its_answer_used(monkeypatch):
    seen = use_service(monkeypatch, lambda r: httpx.Response(200, json=SERVICE_REPLY))
    text, sources = grounding.retrieve("gateway replicas default", ["gateway"])
    assert text == SERVICE_REPLY["rendered"]
    assert sources == [{k: v for k, v in SERVICE_REPLY["passages"][0].items() if k != "text"}]
    (request,) = seen
    assert request.url.path == "/v1/search" and request.headers["Authorization"] == "Bearer k"
    assert json.loads(request.content) == {
        "query": "gateway replicas default",
        "capabilities": ["gateway"],
        "notes_loaded": True,
    }


BUILD_REQUEST = (
    "Add a model gateway. The cluster's kubeconfig is /etc/rancher/rke2/rke2.yaml and our "
    "models are served by vLLM at http://vllm.ai.svc:8000/v1. Gateway API keys are in the "
    "Secret gateway-keys. Database password Kx7-plat-2291, cache password is Vk-551."
)


def test_the_search_query_leaves_out_addresses_paths_and_credentials():
    query = grounding.search_query(BUILD_REQUEST)
    assert "Add a model gateway" in query and "served by vLLM" in query
    for value in ("/etc/rancher", "http://", "Kx7-plat-2291", "Vk-551"):
        assert value not in query


def test_a_secret_name_is_not_a_credential_and_stays_in_the_query():
    assert "gateway-keys" in grounding.search_query(BUILD_REQUEST)


def test_the_service_never_receives_a_password_the_user_typed(monkeypatch):
    seen = use_service(monkeypatch, lambda r: httpx.Response(200, json=SERVICE_REPLY))
    grounding.retrieve(BUILD_REQUEST, ["gateway"])
    assert "Kx7-plat-2291" not in seen[0].content.decode()


def test_a_message_that_is_only_an_address_is_not_searched(monkeypatch):
    use_service(monkeypatch, lambda r: pytest.fail("nothing left to search with"))
    assert grounding.retrieve("http://vllm.ai.svc:8000/v1", ["gateway"]) == ("", [])


def test_a_rejection_lookup_asks_for_fewer_passages(monkeypatch):
    seen = use_service(monkeypatch, lambda r: httpx.Response(200, json=SERVICE_REPLY))
    text = grounding.retrieve_for_rejection("gateway.policy_fail_mode: not one of", ["gateway"])
    body = json.loads(seen[0].content)
    assert text == SERVICE_REPLY["rendered"]
    assert body["query"] == "gateway.policy_fail_mode: not one of"
    assert body["top_k"] == grounding.REJECTION_TOP_K
    assert body["budget_tokens"] == grounding.REJECTION_BUDGET_TOKENS


def test_the_passage_text_is_not_sent_to_the_console(monkeypatch):
    use_service(monkeypatch, lambda r: httpx.Response(200, json=SERVICE_REPLY))
    _, sources = grounding.retrieve("gateway replicas", ["gateway"])
    assert "text" not in sources[0]


def test_a_question_the_service_skipped_adds_nothing(monkeypatch):
    skipped = {"passages": [], "rendered": "", "best_similarity": 0.0, "skipped": "few_terms"}
    use_service(monkeypatch, lambda r: httpx.Response(200, json=skipped))
    assert grounding.retrieve("hi", []) == ("", [])


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(
            503, json={"error": {"message": "nothing indexed", "type": "unavailable"}}
        ),
        lambda r: httpx.Response(
            401, json={"error": {"message": "bad key", "type": "unauthorized"}}
        ),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"unexpected": True}),
    ],
)
def test_a_service_error_or_odd_reply_adds_nothing(monkeypatch, handler):
    use_service(monkeypatch, handler)
    assert grounding.retrieve("gateway replicas", ["gateway"]) == ("", [])


def test_an_unreachable_service_adds_nothing(monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused")

    use_service(monkeypatch, down)
    assert grounding.retrieve("gateway replicas", ["gateway"]) == ("", [])


def test_a_slow_service_adds_nothing(monkeypatch):
    def slow(request):
        raise httpx.ReadTimeout("timed out")

    use_service(monkeypatch, slow)
    assert grounding.retrieve("gateway replicas", ["gateway"]) == ("", [])


def test_enabled_without_a_service_url_adds_nothing(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "1")
    monkeypatch.delenv("RAG_SERVICE_URL", raising=False)
    assert grounding.retrieve("gateway replicas", ["gateway"]) == ("", [])


# ---- what the model sees


async def test_the_model_sees_the_reference_after_the_users_message(model, monkeypatch):
    monkeypatch.setattr(graph, "field_docs", lambda capabilities, **_: (REFERENCE, SOURCES))
    await graph.send_message(uuid.uuid4().hex, "what is the gateway replicas default")
    text = _user_text(model.seen[0])
    assert text.rstrip().endswith(REFERENCE) and "what is the gateway replicas default" in text


async def test_the_reference_is_not_kept_in_the_history(model, monkeypatch):
    monkeypatch.setattr(graph, "field_docs", lambda capabilities, **_: (REFERENCE, SOURCES))
    thread = uuid.uuid4().hex
    await graph.send_message(thread, "what is the gateway replicas default")
    stored = graph.get_state(thread)["messages"]
    assert not any(REFERENCE in str(m.content) for m in stored)


async def test_a_later_turn_gets_only_its_own_reference(model, monkeypatch):
    answers = iter([("first passages", SOURCES), ("", [])])
    monkeypatch.setattr(graph, "field_docs", lambda capabilities, **_: next(answers))
    thread = uuid.uuid4().hex
    await graph.send_message(thread, "what is the gateway replicas default")
    await graph.send_message(thread, "thanks")
    assert not any("first passages" in str(m.content) for m in model.seen[-1])


def test_with_nothing_retrieved_the_messages_are_exactly_what_they_were():
    messages = [SystemMessage(content="s"), HumanMessage(content="hi")]
    assert graph._with_reference(messages, "") is messages
    assert graph._with_reference(messages, None) is messages


def test_the_reference_goes_on_the_latest_user_message_only():
    messages = [HumanMessage(content="one"), AIMessage(content="a"), HumanMessage(content="two")]
    out = graph._with_reference(messages, "REF")
    assert out[0].content == "one" and out[2].content == "two\n\nREF"
    assert messages[2].content == "two"


# ---- a plan the SDK rejected

# A plan that passes the agent's own checks: the gateway with its baseline.
PLAN = {
    "kubeconfig_path": "/etc/rancher/rke2/rke2.yaml",
    "cnpg": {
        "kubeconfig_path": "placeholder",
        "name": "platform-db",
        "database": {"name": "appdb", "owner": "appuser", "password": "Kx7-plat-2291"},
    },
    "valkey": {"kubeconfig_path": "placeholder", "name": "platform-cache"},
    "controlplane": {
        "kubeconfig_path": "placeholder",
        "type": "organization",
        "existing_secret": "org-cp-secrets",
    },
    "gateway": {
        "kubeconfig_path": "placeholder",
        "upstream_url": "http://vllm.ai.svc:8000/v1",
        "api_key_secret": "gateway-keys",
        "org_cp_internal_url": "placeholder",
    },
}


def _plan_result(monkeypatch, args, found="Reference: policy_fail_mode is closed or open."):
    """The tool result the model sees for one plan call, and the lookups made."""
    asked = []
    monkeypatch.setattr(
        graph,
        "retrieve_for_rejection",
        lambda error, skills: asked.append((error, list(skills))) or found,
    )
    call = {"name": "build_full_stack_plan", "args": args, "id": "c1"}
    state = {
        "messages": [
            HumanMessage(content="add a gateway"),
            AIMessage(content="", tool_calls=[call]),
        ],
        "skills": ["gateway"],
        "script": None,
    }
    update = graph.execute_tools(state)
    return json.loads(update["messages"][0].content), asked


def test_an_sdk_rejection_brings_passages_about_it(monkeypatch):
    args = {**PLAN, "gateway": {**PLAN["gateway"], "policy_fail_mode": "sideways"}}
    result, asked = _plan_result(monkeypatch, args)
    assert result["valid"] is False and "policy_fail_mode" in result["error"]
    assert result["reference"] == "Reference: policy_fail_mode is closed or open."
    assert asked == [(result["error"], ["gateway"])]


def test_the_agents_own_checks_bring_no_lookup(monkeypatch):
    args = {k: v for k, v in PLAN.items() if k != "controlplane"}
    result, asked = _plan_result(monkeypatch, args)
    assert result["valid"] is False and "reference" not in result and asked == []


def test_an_accepted_plan_brings_no_lookup(monkeypatch):
    result, asked = _plan_result(monkeypatch, PLAN)
    assert result["valid"] is True and "reference" not in result and asked == []


def test_a_lookup_that_finds_nothing_adds_nothing(monkeypatch):
    args = {**PLAN, "gateway": {**PLAN["gateway"], "policy_fail_mode": "sideways"}}
    result, _ = _plan_result(monkeypatch, args, found="")
    assert result["valid"] is False and "reference" not in result


# ---- the console


async def test_a_sources_event_precedes_the_model_call(model, monkeypatch):
    monkeypatch.setattr(graph, "field_docs", lambda capabilities, **_: (REFERENCE, SOURCES))
    events = [e async for e in graph.stream_events(uuid.uuid4().hex, "what is the gateway replicas default")]
    assert [e["type"] for e in events] == ["sources", "final"]
    assert events[0]["sources"] == SOURCES


async def test_no_sources_event_when_nothing_was_retrieved(model):
    events = [e async for e in graph.stream_events(uuid.uuid4().hex, "hello")]
    assert [e["type"] for e in events] == ["final"]


def _fields_asked(monkeypatch) -> list:
    asked = []
    monkeypatch.setattr(
        graph, "field_docs", lambda capabilities, **_: asked.append(list(capabilities)) or ("", [])
    )
    return asked


async def test_the_fields_read_are_of_the_components_the_message_builds(model, monkeypatch):
    asked = _fields_asked(monkeypatch)
    await graph.send_message(uuid.uuid4().hex, "a valkey cache called sessions")
    assert asked == [["valkey"]]


async def test_the_named_component_comes_before_what_it_depends_on(model, monkeypatch):
    asked = _fields_asked(monkeypatch)
    await graph.send_message(
        uuid.uuid4().hex, "Build a tokenizer. The kubeconfig is /etc/rancher/rke2/rke2.yaml."
    )
    assert asked[0][0] == "tokenizer"
    assert {"enricher", "gateway", "queue", "controlplane", "cnpg", "valkey"} <= set(asked[0])


async def test_a_follow_up_naming_nothing_still_reads_the_threads_components(model, monkeypatch):
    asked = _fields_asked(monkeypatch)
    thread = uuid.uuid4().hex
    await graph.send_message(thread, "a valkey cache called sessions")
    await graph.send_message(thread, "make it bigger")
    assert asked[-1] == ["valkey"]


# ---- which commit the index was built from


STATUS = {"model": "m", "chunks": 3287, "commit": "abc123", "built_at": "2026-09-28T10:00:00+00:00"}


def status_then_search(request):
    if request.url.path == "/v1/status":
        return httpx.Response(200, json=STATUS)
    return httpx.Response(200, json=SERVICE_REPLY)


def test_the_index_commit_is_asked_once_then_searches_go_ahead(monkeypatch, caplog):
    seen = use_service(monkeypatch, status_then_search)
    monkeypatch.setattr(grounding, "_index_checked", False)
    monkeypatch.setattr(grounding, "_indexed_source_changed_since", lambda commit: False)
    caplog.set_level("INFO", logger="orchestration.grounding")
    grounding.retrieve("gateway replicas", ["gateway"])
    grounding.retrieve("gateway replicas again", ["gateway"])
    assert [r.url.path for r in seen] == ["/v1/status", "/v1/search", "/v1/search"]
    assert "commit abc123" in caplog.text and "re-run the RAG indexer" not in caplog.text


def test_a_stale_index_is_warned_about(monkeypatch, caplog):
    use_service(monkeypatch, status_then_search)
    monkeypatch.setattr(grounding, "_index_checked", False)
    monkeypatch.setattr(grounding, "_indexed_source_changed_since", lambda commit: True)
    caplog.set_level("INFO", logger="orchestration.grounding")
    text, _ = grounding.retrieve("gateway replicas", ["gateway"])
    assert text == SERVICE_REPLY["rendered"]
    assert any(r.levelname == "WARNING" and "abc123" in r.message for r in caplog.records)


def test_an_unavailable_status_does_not_stop_the_search(monkeypatch):
    def no_status(request):
        if request.url.path == "/v1/status":
            return httpx.Response(503, json={"error": {"message": "x", "type": "unavailable"}})
        return httpx.Response(200, json=SERVICE_REPLY)

    use_service(monkeypatch, no_status)
    monkeypatch.setattr(grounding, "_index_checked", False)
    assert grounding.retrieve("gateway replicas", ["gateway"])[0] == SERVICE_REPLY["rendered"]


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A git repository shaped like this one, with one commit."""
    (tmp_path / "multistack").mkdir()
    (tmp_path / "multistack" / "spec.py").write_text("replicas = 2\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("notes\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(grounding, "REPO_ROOT", tmp_path)
    return tmp_path, head


def test_unchanged_indexed_source_is_not_stale(checkout):
    _repo, head = checkout
    assert grounding._indexed_source_changed_since(head) is False


def test_an_edit_outside_the_indexed_source_is_not_stale(checkout):
    repo, head = checkout
    (repo / "docs" / "notes.md").write_text("edited\n")
    assert grounding._indexed_source_changed_since(head) is False


def test_an_edit_to_the_indexed_source_is_stale(checkout):
    repo, head = checkout
    (repo / "multistack" / "spec.py").write_text("replicas = 3\n")
    assert grounding._indexed_source_changed_since(head) is True


def test_a_commit_this_checkout_does_not_have_cannot_be_judged(checkout):
    assert grounding._indexed_source_changed_since("0" * 40) is None


def test_a_new_untracked_file_in_the_indexed_source_is_stale(checkout):
    repo, head = checkout
    (repo / "multistack" / "new_spec.py").write_text("x = 1\n")
    assert grounding._indexed_source_changed_since(head) is True


def test_an_index_built_from_uncommitted_changes_is_not_compared(monkeypatch, caplog):
    def handler(request):
        if request.url.path == "/v1/status":
            return httpx.Response(200, json={**STATUS, "commit_modified": True})
        return httpx.Response(200, json=SERVICE_REPLY)

    use_service(monkeypatch, handler)
    monkeypatch.setattr(grounding, "_index_checked", False)
    monkeypatch.setattr(
        grounding, "_indexed_source_changed_since", lambda c: pytest.fail("must not compare")
    )
    caplog.set_level("INFO", logger="orchestration.grounding")
    grounding.retrieve("gateway replicas", ["gateway"])
    assert "not committed" in caplog.text
    assert not any(r.levelname == "WARNING" for r in caplog.records)
