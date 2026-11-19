"""request_check.py: which values it shows the model (secrets included,
unlike source_check's own use of the same chosen_values()), and what it
does with the model's answer."""

from __future__ import annotations

import json

from orchestration.request_check import INSTRUCTIONS, check, message, verify
from orchestration.source_check import chosen_values

from mcp_tools.full_stack import build_full_stack_plan, prepare_args
from test_grounding import PLAN


def answering(text):
    asked = []

    def ask(messages):
        asked.append(messages)
        return text

    return ask, asked


# ---- which values are shown (include_secrets=True, unlike source_check)


def test_a_credential_the_user_never_gave_is_visible_here_unlike_source_check():
    args = prepare_args(
        {**PLAN, "policy": {"kubeconfig_path": "/k", "type": "tpm", "cache_auth_url": "redis://:x@h:6379"}}
    )
    # source_check's own default (include_secrets unset) still hides it --
    # this fix only widens request_check's own view, not source_check's.
    assert not any(n == "cache_auth_url" for _, n, _ in chosen_values(build_full_stack_plan, "build_full_stack_plan", args))
    with_secrets = chosen_values(
        build_full_stack_plan, "build_full_stack_plan", args, include_secrets=True
    )
    assert ("policy", "cache_auth_url", "redis://:x@h:6379") in with_secrets


def test_check_passes_include_secrets_through(monkeypatch):
    seen = {}

    def spy(function, tool_name, args, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr("orchestration.request_check.chosen_fields", spy)
    check(build_full_stack_plan, "build_full_stack_plan", {}, "add a gateway", lambda m: "[]")
    assert seen == {"include_secrets": True}


# ---- a value the script comes out the same without is not the user's to match


def _billing_plan():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))
    from script_cases import SCRIPT_CASES

    case = next(c for c in SCRIPT_CASES if c.id == "billing-meter-name")
    return case, prepare_args(case.expected)


def test_a_storage_class_the_tool_takes_from_the_cluster_is_not_held_against_the_request():
    # Seen live on 2026-10-13: the plan passed queue.storage_class beside
    # existing_storage_class, the check read "Its StorageClass is longhorn"
    # as the cache's, and a correct plan was sent back to ask the user.
    from orchestration.request_check import reaching_the_script
    from orchestration.source_check import chosen_fields

    case, args = _billing_plan()
    args["queue"] = {**args["queue"], "storage_class": "longhorn"}
    chosen = chosen_fields(build_full_stack_plan, "build_full_stack_plan", args, include_secrets=True)
    assert ("queue", "storage_class", "longhorn", ("queue", "storage_class")) in chosen
    kept = [(c, n) for c, n, _, _ in reaching_the_script(build_full_stack_plan, args, chosen)]
    assert ("queue", "storage_class") not in kept
    # What the request did ask for still is.
    assert ("billing", "stripe_meter_event_name") in kept


def test_check_shows_the_model_only_what_reaches_the_script():
    case, args = _billing_plan()
    args["queue"] = {**args["queue"], "storage_class": "longhorn"}
    ask, asked = answering("[]")
    assert check(build_full_stack_plan, "build_full_stack_plan", args, case.request, ask) is None
    shown = asked[0][1].content
    assert "stripe_meter_event_name" in shown
    assert "storage_class" not in shown


def test_every_value_is_compared_when_the_plan_cannot_be_rendered():
    from orchestration.request_check import reaching_the_script

    chosen = [("gateway", "replicas", "9", ("gateway", "replicas"))]
    assert reaching_the_script(build_full_stack_plan, {"gateway": {"replicas": 9}}, chosen) == chosen


def test_a_chosen_value_is_located_in_a_list_and_under_options():
    from orchestration.source_check import chosen_fields

    args = prepare_args(
        {
            **PLAN,
            "gateway": {**PLAN["gateway"], "options": {"max_inflight": 7}},
            "policy": [{"kubeconfig_path": "/k", "type": "rpm", "replicas": 3, "limits": {"user_default": 5}}],
        }
    )
    paths = {(c, n): path for c, n, _, path in chosen_fields(build_full_stack_plan, "build_full_stack_plan", args)}
    assert paths[("gateway", "max_inflight")] == ("gateway", "options", "max_inflight")
    assert paths[("policy", "replicas")] == ("policy", 0, "replicas")


# ---- what the model is told, and what its answer does


def test_a_value_the_user_never_justified_is_reported_with_why():
    ask, asked = answering('[{"setting": 1, "why": "the user never asked for 99999 replicas"}]')
    chosen = [("gateway", "replicas", "99999")]
    [flag] = verify(chosen, "add a gateway for my vllm server", ask)
    assert flag == {
        "capability": "gateway",
        "name": "replicas",
        "value": "99999",
        "why": "the user never asked for 99999 replicas",
    }
    assert "99999" in asked[0][1].content
    assert "add a gateway for my vllm server" in asked[0][1].content


def test_an_out_of_range_setting_number_is_dropped():
    ask, _ = answering('[{"setting": 7, "why": "nonsense"}]')
    assert verify([("gateway", "replicas", "3")], "add a gateway", ask) == []


def test_no_chosen_values_or_no_request_text_skips_the_call_entirely():
    ask, asked = answering("[]")
    assert verify([], "add a gateway", ask) == []
    assert verify([("gateway", "replicas", "3")], "", ask) == []
    assert asked == []  # never called either way -- nothing to check


def test_a_malformed_answer_fails_open_not_closed():
    ask, _ = answering("not json at all")
    assert verify([("gateway", "replicas", "3")], "add a gateway", ask) == []


def test_message_names_the_field_and_the_reason():
    flags = [{"capability": "policy", "name": "cache_auth_url", "value": "redis://:x@h", "why": "no cache url was given"}]
    text = message(flags)
    assert "policy.cache_auth_url=" in text
    assert "no cache url was given" in text


# ---- the instructions no longer give unmentioned values a free pass


def test_instructions_no_longer_exempt_every_unmentioned_setting():
    # The old wording ("a setting the user never mentioned... is never a
    # mismatch") is exactly the gap that let an unrequested replicas=99999
    # through uncaught -- this pins that it's gone, not just reworded.
    assert "never mentioned at all is the system" not in INSTRUCTIONS
    assert "no basis in the request" in INSTRUCTIONS or "nothing to justify it" in INSTRUCTIONS
