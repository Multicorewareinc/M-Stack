"""A generated evaluation set (tests/eval/fresh_cases.py) is held to the
checks the hand-written one passes, for more than one seed: every expected
plan validates against the real tools with its dependency chain and sets
what its request asks for, every request states the values only the user
can give and is offered the tool its plan calls, every ask case is rejected
for what its reply must ask for, and every pause case stops on its options
and needs nothing more once its answers are in."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

from fresh_cases import SEED, generate, passage
from orchestration import needs
from orchestration.knowledge import load_skills
from orchestration.selection import select
from script_cases import FULL_STACK, SCRIPT_CASES, WIRED, reference_plan, tool_function

REPO_ROOT = Path(__file__).resolve().parents[3]
# The generator's own seed, and the one the 2026-10-13 evaluation ran on.
SEEDS = (SEED, 218728121)
CASES = [case for seed in SEEDS for case in generate(seed)]
IDS = [f"{seed}:{case.id}" for seed in SEEDS for case in generate(seed)]
BUILD = [(i, c) for i, c in zip(IDS, CASES) if not c.asks and not c.pauses]
ASK = [(i, c) for i, c in zip(IDS, CASES) if c.asks]
PAUSE = [(i, c) for i, c in zip(IDS, CASES) if c.pauses]
SINGLE = [*BUILD, *ASK]
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
        "address",
        "ssh_key",
    }
)


def _user_values(args) -> list[str]:
    if isinstance(args, dict):
        found = [v for k, v in args.items() if k in _USER_KEYS and isinstance(v, str) and v != WIRED]
        return found + [v for value in args.values() for v in _user_values(value)]
    if isinstance(args, list):
        return [v for value in args for v in _user_values(value)]
    return []


def _keys(args) -> set[str]:
    if isinstance(args, dict):
        return set(args) | {k for value in args.values() for k in _keys(value)}
    if isinstance(args, list):
        return {k for value in args for k in _keys(value)}
    return set()


def _params(cases):
    return [pytest.param(case, id=case_id) for case_id, case in cases]


def test_a_seed_always_gives_the_same_set_and_another_seed_a_different_one():
    generate.cache_clear()
    again = generate(SEED)
    assert [(c.id, c.request, c.expected) for c in again] == [
        (c.id, c.request, c.expected) for c in CASES[: len(again)]
    ]
    assert {c.request for c in generate(SEED)} != {c.request for c in generate(SEED + 1)}


def test_a_set_has_every_kind_of_case_and_none_of_the_hand_written_requests():
    for seed in SEEDS:
        cases = generate(seed)
        assert len({c.id for c in cases}) == len(cases) == 40
        assert sum(1 for c in cases if c.pauses) == 5
        assert sum(1 for c in cases if c.asks) == 8
        assert not {c.request for c in cases} & {c.request for c in SCRIPT_CASES}


@pytest.mark.parametrize("case", _params(BUILD))
def test_expected_plan_validates_with_its_dependency_chain_and_sets_what_was_asked(case):
    result = reference_plan(case)
    assert result["valid"] is True, result.get("error")
    if case.tool == FULL_STACK:
        assert {layer.split(":")[0] for layer in result["layers"]} - {"rke2"} == set(case.layers)
    assert [line for line in case.must_set if line not in result["script"]] == []


@pytest.mark.parametrize("case", _params(SINGLE))
def test_the_case_uses_the_tool_its_request_is_offered(case):
    _, tools = select(case.request)
    assert case.tool in tools
    if case.tool != FULL_STACK:
        assert FULL_STACK not in tools


@pytest.mark.parametrize("case", _params(SINGLE))
def test_request_states_every_value_the_user_must_supply(case):
    assert [v for v in _user_values(case.expected) if v not in case.request] == []


@pytest.mark.parametrize("case", _params(ASK))
def test_an_ask_case_is_rejected_for_what_its_reply_must_ask_for(case):
    if case.asks_because:
        # The tool builds it; the platform's own notes say to ask first.
        assert any(case.asks_because in content for _, content in load_skills())
        return
    result = reference_plan(case)
    assert result["valid"] is False
    assert any(word in result["error"].lower() for word in case.ask_for), result["error"]


@pytest.mark.parametrize("case", _params([(i, c) for i, c in BUILD if c.reference_context_ids]))
def test_each_reference_passage_defines_a_field_the_plan_sets(case):
    for ref in case.reference_context_ids:
        path, span = ref.rsplit(":", 1)
        start, end = (int(n) for n in span.split("-"))
        lines = (REPO_ROOT / path).read_text().splitlines()
        assert 1 <= start <= end <= len(lines), ref
        fields = {m.group(1) for line in lines[start - 1 : end] if (m := _FIELD.match(line))}
        assert fields & _keys(case.expected), ref


def test_a_reference_passage_is_the_one_rag_cuts_for_that_field():
    # The same passage the hand-written set names for these fields.
    assert passage("Gateway.replicas") == "multistack/gateway/spec.py:140-140"
    assert passage("Observability.metrics_retention") == "multistack/observability/spec.py:93-93"


@pytest.mark.parametrize("case", _params(PAUSE))
def test_a_pause_cases_plan_validates_and_its_conversation_states_every_value(case):
    result = reference_plan(case)
    assert result["valid"] is True, result.get("error")
    assert [line for line in case.must_set if line not in result["script"]] == []
    assert [v for v in _user_values(case.expected) if v not in case.said] == []
    # The request alone does not: that is why the agent has to stop.
    assert case.first[1] != case.expected


@pytest.mark.parametrize("case", _params(PAUSE))
def test_the_call_the_request_alone_supports_pauses_with_the_cases_first_options(case):
    tool, args = case.first
    report = needs.collect(
        tool_function(tool), tool, args, case.request, skills=list(case.layers), ask=lambda messages: "[]"
    )
    assert report.asks_user and report.choices, report.message()
    assert report.choices[0].options == case.pauses[0]
    assert len(case.pauses) <= len(case.answers)


@pytest.mark.parametrize("case", _params(PAUSE))
def test_the_whole_conversation_needs_nothing_more(case):
    def never(messages):
        raise AssertionError("the answers state the choice; nothing left to judge")

    report = needs.collect(
        tool_function(case.tool), case.tool, case.expected, case.said, skills=list(case.layers), ask=never
    )
    assert not report.blocked, report.message()


@pytest.mark.parametrize("case", _params(PAUSE))
def test_a_pause_cases_tool_is_on_offer_once_its_answers_are_in(case):
    skills, tools = select(case.request)
    for answer in case.answers:
        skills, tools = select(answer, skills)
    assert case.tool in tools
