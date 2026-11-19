"""What ragas_script_prepare.py treats as the same tool call and what as a
different one: RAGAS compares the flattened fields one by one, so a wrong
field or an extra component must show as a difference, and a default
spelled out or a wired value must not."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))

import json

from ragas_script_prepare import TOOL, call_fields, failure, plan_fields, sample, samples
from script_cases import SCRIPT_CASES

CASES = {c.id: c for c in SCRIPT_CASES}


def _call(args: dict) -> dict:
    return {"name": TOOL, "args": plan_fields(args)}


def _differing(a: dict, b: dict) -> set[str]:
    return {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}


def test_every_reference_call_flattens_and_names_its_components():
    for case in SCRIPT_CASES:
        if case.asks or case.tool != TOOL:
            continue
        fields = plan_fields(case.expected)
        assert fields["components"] == ",".join(
            sorted(k for k, v in case.expected.items() if isinstance(v, dict))
        )


def test_every_reference_call_compares_equal_to_itself_with_no_failure():
    for case in SCRIPT_CASES:
        if not case.asks:
            reference = {"name": case.tool, "args": call_fields(case.tool, case.expected)}
            assert failure(reference, reference) is None, case.id


def test_a_changed_value_differs_in_that_field_only():
    case = CASES["gateway-fail-open"]
    args = copy.deepcopy(case.expected)
    args["gateway"]["policy_fail_mode"] = "closed"
    assert _differing(plan_fields(case.expected), plan_fields(args)) == {"gateway.policy_fail_mode"}


def test_a_component_nobody_asked_for_changes_the_components_entry():
    case = CASES["gateway-slow-models"]
    args = copy.deepcopy(case.expected)
    args["policy"] = {
        "kubeconfig_path": "placeholder",
        "cache_url": "redis://valkey:6379/0",
        "allow_no_backbone": True,
        "limits": {"user_default": 60},
    }
    assert "components" in _differing(plan_fields(case.expected), plan_fields(args))


def test_a_default_spelled_out_or_a_wired_value_is_not_a_difference():
    case = CASES["queue-volume"]
    args = copy.deepcopy(case.expected)
    args["queue"]["options"]["replicas"] = 3
    args["queue"]["kubeconfig_path"] = "anything"
    args["queue"]["storage_class"] = "anything"
    assert _differing(plan_fields(case.expected), plan_fields(args)) == set()


def test_a_component_sent_as_null_is_the_same_call_as_leaving_it_out():
    # The model sometimes sends every component, unused ones as null.
    case = CASES["gateway-replicas"]
    args = {**copy.deepcopy(case.expected), "billing": None, "accelerator": None}
    assert plan_fields(args) == plan_fields(case.expected)
    cpu = CASES["serve-on-cpu"]
    assert call_fields(cpu.tool, {**cpu.expected, "nodes": None}) == call_fields(
        cpu.tool, cpu.expected
    )


def test_a_namespace_spelled_out_at_its_default_is_not_a_difference():
    # Seen live: the model set queue.namespace="platform", where an empty
    # namespace deploys anyway.
    case = CASES["billing-meter-name"]
    args = copy.deepcopy(case.expected)
    args["queue"]["namespace"] = "platform"
    assert _differing(plan_fields(case.expected), plan_fields(args)) == set()
    args["queue"]["namespace"] = "events"
    assert _differing(plan_fields(case.expected), plan_fields(args)) == {"queue.namespace"}


def test_a_one_item_list_is_the_same_call_as_the_component():
    case = CASES["chain-gateway"]
    args = copy.deepcopy(case.expected)
    args["controlplane"] = [args["controlplane"]]
    assert _differing(plan_fields(case.expected), plan_fields(args)) == set()


def test_the_last_accepted_plan_is_the_call_scored():
    case = CASES["gateway-fail-open"]
    rejected = {"tool": "build_full_stack_plan", "args": {}, "valid": False}
    accepted = {"tool": "build_full_stack_plan", "args": case.expected, "valid": True}
    record = {"attempts": [rejected, accepted], "sources": ["a.py:1-2"]}
    s = sample(case, record)
    assert s["response_tool_call"]["args"] == s["reference_tool_call"]["args"]
    assert s["retrieved_context_ids"] == ["a.py:1-2"]
    assert s["reference_context_ids"] == list(case.reference_context_ids)


def test_a_case_without_an_accepted_plan_has_no_tool_call():
    case = CASES["gateway-fail-open"]
    record = {"attempts": [{"tool": "build_full_stack_plan", "args": {}, "valid": False}]}
    s = sample(case, record)
    assert s["response_tool_call"] is None
    assert s["failure"] == "no valid plan"


def test_a_right_call_has_no_failure():
    case = CASES["gateway-fail-open"]
    assert failure(_call(case.expected), _call(case.expected)) is None


def test_a_wrong_value_names_the_field_and_the_expected_value():
    case = CASES["gateway-fail-open"]
    args = copy.deepcopy(case.expected)
    args["gateway"]["policy_fail_mode"] = "closed"
    assert failure(_call(args), _call(case.expected)) == (
        "wrong value: gateway.policy_fail_mode='closed' (expected 'open')"
    )


def test_an_extra_component_is_named_without_listing_its_fields():
    # What the pilot's RAG-on run did: the request mentions the rate-limit
    # service, and the model composed it.
    case = CASES["gateway-fail-open"]
    args = copy.deepcopy(case.expected)
    args["policy"] = {
        "kubeconfig_path": "placeholder",
        "cache_url": "redis://valkey:6379/0",
        "allow_no_backbone": True,
    }
    assert failure(_call(args), _call(case.expected)) == "extra component: policy"


def test_a_missing_component_is_named():
    case = CASES["gateway-fail-open"]
    args = copy.deepcopy(case.expected)
    del args["gateway"]
    assert failure(_call(args), _call(case.expected)) == "missing component: gateway"


def test_a_different_tool_is_named():
    case = CASES["queue-volume"]
    got = {"name": "build_queue_plan", "args": {}}
    assert failure(got, _call(case.expected)) == "wrong tool: build_queue_plan"


def _single(case_id: str, args: dict | None = None) -> dict:
    case = CASES[case_id]
    return {"name": case.tool, "args": call_fields(case.tool, args or case.expected)}


def test_a_single_resource_call_is_compared_with_its_defaults_filled_in():
    case = CASES["postgres-instances"]
    args = copy.deepcopy(case.expected)
    args["cnpg"]["storage_size"] = "10Gi"  # the SDK default, spelled out
    assert failure(_single(case.id, args), _single(case.id)) is None
    args["cnpg"]["instances"] = 3
    assert failure(_single(case.id, args), _single(case.id)) == (
        "wrong value: cnpg.instances=3 (expected 2)"
    )


def test_a_single_resource_call_keeps_the_users_kubeconfig():
    # Nothing is wired for a single-resource tool: a wrong path is wrong.
    case = CASES["valkey-chart-version"]
    args = copy.deepcopy(case.expected)
    args["valkey"]["kubeconfig_path"] = "/root/.kube/config"
    assert failure(_single(case.id, args), _single(case.id)).startswith(
        "wrong value: valkey.kubeconfig_path="
    )


def test_a_node_list_is_compared_node_by_node():
    case = CASES["cluster-cilium"]
    args = copy.deepcopy(case.expected)
    args["cluster"]["nodes"][2]["role"] = "server"
    assert failure(_single(case.id, args), _single(case.id)) == (
        "wrong value: cluster.nodes[10.0.0.13].role='server' (expected 'agent')"
    )


def _ask(case_id: str, attempts: list[dict], reply: str | None) -> dict:
    return sample(CASES[case_id], {"attempts": attempts, "reply": reply})


def test_an_ask_case_is_right_when_nothing_is_built_and_the_reply_asks():
    rejected = {"tool": TOOL, "args": {}, "valid": False}
    s = _ask("ask-db-password", [rejected], "What password should the appdb database use?")
    assert s["ask"] is True and s["reference_tool_call"] is None
    assert s["failure"] is None


def test_an_ask_case_that_builds_is_wrong_whatever_the_reply():
    case = CASES["ask-db-password"]
    built = copy.deepcopy(case.expected)
    built["cnpg"]["database"]["password"] = "S3cure-Pass-1"
    accepted = {"tool": TOOL, "args": built, "valid": True}
    assert _ask(case.id, [accepted], "Built it.")["failure"] == (
        "built instead of asking: build_full_stack_plan"
    )


def test_an_ask_case_whose_reply_asks_for_something_else_is_wrong():
    s = _ask("ask-db-password", [], "Which StorageClass does the cluster have?")
    assert s["failure"].startswith("did not ask for the missing value")


def test_runs_that_never_reached_the_agent_are_left_out_and_counted():
    accepted = {"tool": TOOL, "args": CASES["gateway-fail-open"].expected, "valid": True}
    lines = [
        json.dumps({"id": "gateway-fail-open", "run": 2, "attempts": [accepted]}),
        json.dumps({"id": "gateway-fail-open", "run": 1, "error": "connection refused"}),
        json.dumps({"id": "queue-volume", "run": 1, "error": "connection refused"}),
        # The redone run replaces the outage.
        json.dumps({"id": "gateway-fail-open", "run": 1, "attempts": [accepted]}),
    ]
    out, unreached = samples(lines)
    assert [(s["id"], s["run"]) for s in out] == [
        ("gateway-fail-open", 1),
        ("gateway-fail-open", 2),
    ]
    assert unreached == 1
