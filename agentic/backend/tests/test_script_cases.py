"""The script-generation evaluation set stays valid against the real plan
tools: every expected plan validates, contains its dependency chain, sets
what its request asks for, uses the one tool its request is offered, its
request states every value the user must supply, each passage RAG must
return still defines a field the plan sets, and every ask case is rejected
for the very value its reply must ask for."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

from orchestration.knowledge import load_skills
from orchestration.selection import select
from script_cases import ALL_CASES, FULL_STACK, PAUSE_CASES, SCRIPT_CASES, WIRED, reference_plan

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD = [c for c in SCRIPT_CASES if not c.asks]
ASK = [c for c in SCRIPT_CASES if c.asks]
_FIELD = re.compile(r"\s*(\w+)\s*:")

# Values only the user can give; the agent must never make them up.
_USER_KEYS = frozenset(
    {
        "kubeconfig_path",
        "existing_storage_class",
        "upstream_url",
        "api_key_secret",
        "existing_secret",
        "name",
        "owner",
        "password",
    }
)


def _user_values(args) -> list[str]:
    if isinstance(args, dict):
        found = [
            v for k, v in args.items() if k in _USER_KEYS and isinstance(v, str) and v != WIRED
        ]
        return found + [v for value in args.values() for v in _user_values(value)]
    return []


def test_case_ids_are_unique():
    ids = [c.id for c in ALL_CASES]
    assert len(ids) == len(set(ids))


def test_the_set_has_held_out_and_ask_cases():
    assert len(SCRIPT_CASES) == 43
    assert sum(c.held_out for c in SCRIPT_CASES) == 13
    assert len(ASK) == 9


@pytest.mark.parametrize("case", BUILD, ids=lambda c: c.id)
def test_expected_plan_validates_with_its_dependency_chain(case):
    result = reference_plan(case)
    assert result["valid"] is True, result.get("error")
    if case.tool == FULL_STACK:
        # route layers are named after the route: "route:chat".
        assert {layer.split(":")[0] for layer in result["layers"]} - {"rke2"} == set(case.layers)


@pytest.mark.parametrize("case", BUILD, ids=lambda c: c.id)
def test_reference_script_sets_what_the_request_asks_for(case):
    script = reference_plan(case)["script"]
    assert [line for line in case.must_set if line not in script] == []


@pytest.mark.parametrize("case", SCRIPT_CASES, ids=lambda c: c.id)
def test_the_case_uses_a_tool_its_request_is_offered(case):
    # A component without dependencies is offered only its own tool, so
    # that tool is the one right answer.
    _, tools = select(case.request)
    assert case.tool in tools
    if case.tool != FULL_STACK:
        assert FULL_STACK not in tools


@pytest.mark.parametrize("case", ASK, ids=lambda c: c.id)
def test_an_ask_case_is_rejected_for_the_value_its_reply_must_ask_for(case):
    result = reference_plan(case)
    if case.asks_because:
        # The tool builds it; the platform's own notes, or the source the
        # case's passage comes from, say not to.
        sources = [content for _, content in load_skills()] + [
            (REPO_ROOT / ref.rsplit(":", 1)[0]).read_text() for ref in case.reference_context_ids
        ]
        assert any(case.asks_because in text for text in sources)
        return
    assert result["valid"] is False
    assert any(word in result["error"].lower() for word in case.ask_for), result["error"]


@pytest.mark.parametrize("case", SCRIPT_CASES, ids=lambda c: c.id)
def test_request_states_every_value_the_user_must_supply(case):
    assert [v for v in _user_values(case.expected) if v not in case.request] == []


def _keys(args) -> set[str]:
    if isinstance(args, dict):
        return set(args) | {k for value in args.values() for k in _keys(value)}
    return set()


@pytest.mark.parametrize("case", [c for c in BUILD if c.reference_context_ids], ids=lambda c: c.id)
def test_each_reference_passage_defines_a_field_the_plan_sets(case):
    # An edit to the SDK that shifts its lines would otherwise leave the
    # retrieval references pointing at the wrong passage, scoring RAG as
    # missing what it found.
    for ref in case.reference_context_ids:
        path, span = ref.rsplit(":", 1)
        start, end = (int(n) for n in span.split("-"))
        lines = (REPO_ROOT / path).read_text().splitlines()
        assert 1 <= start <= end <= len(lines), ref
        # A field's passage can run past its first line (a Field(...) with
        # a description) or start with its comment.
        fields = {m.group(1) for line in lines[start - 1 : end] if (m := _FIELD.match(line))}
        assert fields & _keys(case.expected), ref



# ---- pause cases: a short conversation, with a decision the agent has to
# stop and offer as options


def test_no_single_request_case_pauses():
    assert not [c.id for c in SCRIPT_CASES if c.pauses or c.answers]
    assert all(c.pauses and c.answers and c.first for c in PAUSE_CASES)


@pytest.mark.parametrize("case", PAUSE_CASES, ids=lambda c: c.id)
def test_a_pause_cases_expected_plan_validates_and_sets_what_was_asked(case):
    result = reference_plan(case)
    assert result["valid"] is True, result.get("error")
    assert [line for line in case.must_set if line not in result["script"]] == []


@pytest.mark.parametrize("case", PAUSE_CASES, ids=lambda c: c.id)
def test_a_pause_cases_conversation_states_every_value_the_user_must_supply(case):
    assert [v for v in _user_values(case.expected) if v not in case.said] == []
    # The request alone does not: that is why the agent has to stop.
    tool, args = case.first
    assert args != case.expected


@pytest.mark.parametrize("case", PAUSE_CASES, ids=lambda c: c.id)
def test_the_call_the_request_alone_supports_pauses_with_the_cases_options(case):
    from orchestration import needs
    from script_cases import tool_function

    def unstated(messages):
        return "[]"

    tool, args = case.first
    report = needs.collect(tool_function(tool), tool, args, case.request, skills=list(case.layers), ask=unstated)
    assert report.asks_user and report.choices, report.message()
    assert report.choices[0].options == case.pauses[0]


@pytest.mark.parametrize("case", PAUSE_CASES, ids=lambda c: c.id)
def test_the_whole_conversation_needs_nothing_more(case):
    from orchestration import needs
    from script_cases import tool_function

    def never(messages):
        raise AssertionError("the answers state the choice; nothing left to judge")

    report = needs.collect(
        tool_function(case.tool), case.tool, case.expected, case.said, skills=list(case.layers), ask=never
    )
    assert not report.blocked, report.message()


@pytest.mark.parametrize("case", PAUSE_CASES, ids=lambda c: c.id)
def test_a_pause_cases_tool_is_on_offer_once_its_answers_are_in(case):
    # Each answer is selected for like a new message, on top of what the
    # conversation already holds -- the way the agent takes it in.
    skills, tools = select(case.request)
    for answer in case.answers:
        skills, tools = select(answer, skills)
    assert case.tool in tools


# ---- which cluster a request means, in the words requests use


@pytest.mark.parametrize(
    "said",
    [
        "Build a new RKE2 cluster called lab with a server node at 10.0.0.11",
        "Create a new cluster",
        "Create a new 3-node RKE2 cluster named edge",
        "We want a brand new Kubernetes cluster for this",
        "a new, small cluster for the lab",
    ],
)
def test_a_new_cluster_is_recognised(said):
    from orchestration.selection import onto_existing_cluster, wants_new_cluster

    assert wants_new_cluster(said) and not onto_existing_cluster(said)


@pytest.mark.parametrize(
    "said",
    [
        # A database's own cluster is that component, not a cluster to build.
        "We need a new Postgres cluster named mcw-postgres. The cluster's kubeconfig is /etc/rancher/rke2/rke2.yaml.",
        "Add a new Vault cluster with 5 replicas; kubeconfig /etc/rancher/rke2/rke2.yaml",
        # "new" belongs to the component, the cluster is where it goes.
        "Add a new cache to the cluster whose kubeconfig is /root/.kube/config.",
        "I need a new Valkey cache. Cluster kubeconfig is /x/y.",
    ],
)
def test_a_new_component_on_the_users_cluster_is_not_a_new_cluster(said):
    from orchestration.selection import onto_existing_cluster, wants_new_cluster

    assert not wants_new_cluster(said) and onto_existing_cluster(said)
