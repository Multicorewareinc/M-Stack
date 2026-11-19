"""orchestration/needs.py: one pass over a tool call reports everything
it still needs -- values from the user, choices for the user, corrections
for the model -- from the SDK's own validation and the tool's own rules.

Offline: no model and no retrieval. Where a judgement call is involved it
is a plain function passed in."""

from __future__ import annotations

import pytest

from orchestration import needs
from orchestration.tools import ALL_TOOLS

TOOLS = {t.name: t for t in ALL_TOOLS}
KUBECONFIG = "/etc/rancher/rke2/rke2.yaml"
EXISTING = f"The cluster's kubeconfig is {KUBECONFIG}."


def collect(tool: str, args: dict, said: str, **kwargs) -> needs.Report:
    return needs.collect(TOOLS[tool].func, tool, args, said, **kwargs)


def paths(found) -> list[str]:
    return [n.path for n in found]


# ---- which cluster: asked as a choice, never assumed


def test_no_word_about_the_cluster_is_a_choice_not_a_missing_field():
    report = collect("build_valkey_plan", {"valkey": {"name": "sessions"}}, "add a valkey cache called sessions")
    assert [c.key for c in report.choices] == ["cluster"]
    assert report.choices[0].options == (needs.NEW_CLUSTER, needs.EXISTING_CLUSTER)
    assert report.values == [] and report.fixes == []
    assert report.asks_user


def test_the_same_choice_for_any_component_and_for_the_whole_stack_tool():
    for tool, args in (
        ("build_inference_plan", {"inference": {}}),
        ("build_cnpg_plan", {"cnpg": {}}),
        ("build_full_stack_plan", {"valkey": {"name": "sessions"}, "gateway": {}}),
    ):
        report = collect(tool, args, "build it")
        assert [c.key for c in report.choices] == ["cluster"], tool


def test_an_existing_cluster_without_its_kubeconfig_is_a_value_to_ask_for():
    report = collect(
        "build_valkey_plan", {"valkey": {"name": "sessions"}},
        "add a valkey cache called sessions\nUse an existing cluster",
    )
    assert report.choices == []
    assert paths(report.values) == ["valkey.kubeconfig_path"]
    assert "kubeconfig" in report.values[0].note


def test_a_new_cluster_sends_a_single_component_to_the_whole_stack_tool():
    report = collect(
        "build_valkey_plan", {"valkey": {"name": "sessions"}},
        "add a valkey cache called sessions\nCreate a new cluster",
    )
    assert report.choices == [] and report.values == []
    assert "build_full_stack_plan" in report.fixes[0] and "cluster" in report.fixes[0]


def test_a_cluster_being_composed_answers_the_choice():
    report = collect(
        "build_full_stack_plan",
        {"cluster": {"name": "demo", "nodes": [{"address": "10.0.0.1", "user": "root", "role": "server"}]},
         "valkey": {"name": "sessions"}},
        "a cluster called demo at 10.0.0.1 with a valkey cache called sessions",
    )
    assert report.choices == []
    assert not report.blocked


def test_a_field_passed_as_an_empty_string_was_not_given():
    report = collect("build_valkey_plan", {"valkey": {"kubeconfig_path": "", "name": " "}}, EXISTING)
    assert paths(report.values) == ["valkey.name", "valkey.kubeconfig_path"]


# ---- everything in one pass


def test_a_complete_call_needs_nothing():
    report = collect("build_valkey_plan", {"valkey": {"kubeconfig_path": KUBECONFIG, "name": "sessions"}}, EXISTING)
    assert not report.blocked
    assert report.args == {"valkey": {"kubeconfig_path": KUBECONFIG, "name": "sessions"}}


def test_every_value_a_composed_chain_needs_is_reported_together():
    # Before: the limit, the dependency chain, the storage class, the
    # names and secrets, and the cache password each surfaced only after
    # the one before it had been supplied.
    args = {
        "kubeconfig_path": KUBECONFIG, "policy": {"type": "rpm"}, "gateway": {}, "valkey": {},
        "queue": {}, "controlplane": {"type": "organization"}, "cnpg": {},
    }
    report = collect("build_full_stack_plan", args, f"add rpm rate limiting. {EXISTING}", skills=["policy"])
    assert report.fixes == [] and report.choices == []
    assert set(paths(report.values)) == {
        "policy.limits",
        "existing_storage_class",
        "gateway.upstream_url",
        "gateway.api_key_secret",
        "valkey.name",
        "controlplane.existing_secret",
        "policy.cache_auth_url",
        "cnpg.database",
    }


def test_a_missing_chain_goes_to_the_model_with_what_it_will_need():
    args = {"kubeconfig_path": KUBECONFIG, "policy": {"type": "rpm"}}
    report = collect("build_full_stack_plan", args, f"add rate limiting. {EXISTING}", skills=["policy"])
    assert len(report.fixes) == 1
    for component in ("cnpg", 'controlplane (type="organization")', "gateway", "queue", "valkey"):
        assert component in report.fixes[0]
    assert not report.asks_user
    assert {"policy.limits", "existing_storage_class"} <= set(paths(report.values))
    assert {"gateway.upstream_url", "gateway.api_key_secret", "valkey.name"} <= set(paths(report.after_fix))
    message = report.message()
    assert "gateway.api_key_secret" in message and "the user will be asked for the rest" in message


def test_a_wired_field_is_never_a_value_to_ask_for():
    from mcp_tools.full_stack import WIRED_FIELDS

    for args in (
        {"kubeconfig_path": KUBECONFIG, "policy": {"type": "rpm"}},
        {"kubeconfig_path": KUBECONFIG, "gateway": {}, "enricher": {}},
        {"kubeconfig_path": KUBECONFIG, "gateway": {}, "policy": {}, "valkey": {}, "queue": {}},
    ):
        report = collect("build_full_stack_plan", args, EXISTING, skills=["policy"])
        asked = paths(report.values) + paths(report.after_fix)
        assert not [p for p in asked if p.rsplit(".", 1)[-1] in WIRED_FIELDS], asked


def test_a_stand_in_value_is_asked_for_again():
    args = {"valkey": {"name": "<name>"}, "kubeconfig_path": KUBECONFIG}
    report = collect("build_full_stack_plan", args, EXISTING)
    assert paths(report.values) == ["valkey.name"]
    assert "stand-in" in report.values[0].note


def test_what_the_sdk_rejects_outright_is_for_the_model_to_correct():
    args = {"valkey": {"kubeconfig_path": KUBECONFIG, "name": "sessions", "no_such_field": 1}}
    report = collect("build_valkey_plan", args, EXISTING)
    assert report.values == []
    assert report.fixes == report.sdk and "valkey.no_such_field" in report.fixes[0]


def test_a_rate_limiter_nobody_asked_for_is_only_told_to_go():
    # Its dependencies would bury that under requirements of a component
    # that shouldn't be in the plan at all.
    args = {"kubeconfig_path": KUBECONFIG, "gateway": {}, "policy": {"type": "rpm"}}
    report = collect("build_full_stack_plan", args, f"a gateway that fails open. {EXISTING}", skills=["gateway"])
    assert len(report.fixes) == 1 and "no real limit" in report.fixes[0]
    assert report.values == [] and report.after_fix == []


def test_a_new_cluster_on_the_users_existing_one_is_refused():
    args = {"cluster": {"name": "x"}, "storage": {"type": "longhorn"}}
    report = collect("build_full_stack_plan", args, f"Add a gateway. {EXISTING}")
    assert any("already exists" in fix and "cluster and storage" in fix for fix in report.fixes)


# ---- a choice a skill says the user has to make


@pytest.fixture
def policy_type_must_be_asked(monkeypatch):
    monkeypatch.setattr(
        needs, "must_ask",
        lambda: {"policy": ("type", "Requests or tokens?", {"tpm": ["tokens per minute"]})},
    )
    needs._stated_memo.clear()


POLICY = {"kubeconfig_path": KUBECONFIG, "policy": {"limits": {"user_default": 60}}}


def choice_keys(said: str, args: dict = POLICY, ask=None) -> tuple[list[str], list[str]]:
    report = collect("build_full_stack_plan", args, said, skills=["policy"], ask=ask)
    return [c.key for c in report.choices], [f for f in report.fixes if "policy.type" in f]


def test_an_option_the_user_wrote_out_needs_no_judgement(policy_type_must_be_asked):
    def never(messages):
        raise AssertionError("the option is in the text; nothing to judge")

    assert choice_keys(f"limit to 60 rpm. {EXISTING}", ask=never) == ([], [])


def test_an_unstated_type_is_asked_as_a_choice_with_the_sdks_own_options(policy_type_must_be_asked):
    report = collect("build_full_stack_plan", POLICY, f"add rate limiting. {EXISTING}", skills=["policy"], ask=lambda m: "[]")
    choice = next(c for c in report.choices if c.key == "policy.type")
    assert choice.options == ("rpm", "tpm")
    assert choice.question == "Requests or tokens?"


def test_a_type_the_user_described_in_other_words_is_not_asked(policy_type_must_be_asked):
    seen = []

    def judge(messages):
        seen.append(messages[1].content)
        return '["rpm"]'

    assert choice_keys(f"60 requests per minute per user. {EXISTING}", ask=judge) == ([], [])
    assert "60 requests per minute per user" in seen[0] and '["rpm", "tpm"]' in seen[0]


def test_a_plan_that_contradicts_the_stated_type_is_corrected(policy_type_must_be_asked):
    choices, fixes = choice_keys(f"limit tokens, tpm. {EXISTING}")
    assert choices == []
    assert len(fixes) == 1 and "'rpm'" in fixes[0] and "tpm" in fixes[0]


def test_without_a_usable_judgement_the_choice_is_asked_not_assumed(policy_type_must_be_asked):
    # Asking once too often costs a click; assuming costs a wrong plan.
    def failing(messages):
        raise RuntimeError("model server unreachable")

    for ask in (None, lambda m: "no idea", failing):
        needs._stated_memo.clear()
        assert choice_keys(f"add rate limiting. {EXISTING}", ask=ask) == (["policy.type"], [])


def test_the_judges_answer_is_read_however_it_quotes_the_options(policy_type_must_be_asked):
    for answer in ("['tpm']", "[tpm]", 'Answer: ["tpm"]'):
        needs._stated_memo.clear()
        choices, fixes = choice_keys(f"cap usage by tokens. {EXISTING}", ask=lambda m, a=answer: a)
        assert choices == [] and len(fixes) == 1 and "tpm" in fixes[0], answer


def test_the_judgement_for_one_conversation_is_made_once(policy_type_must_be_asked):
    calls = []

    def judge(messages):
        calls.append(1)
        return "[]"

    for _ in range(3):
        choice_keys(f"add rate limiting. {EXISTING}", ask=judge)
    assert len(calls) == 1


def test_a_wording_the_skill_declares_states_the_option_without_a_judgement(policy_type_must_be_asked):
    def never(messages):
        raise AssertionError("the skill's own wording is in the text; nothing to judge")

    choices, fixes = choice_keys(f"limit llama to 50000 tokens-per-minute. {EXISTING}", ask=never)
    assert choices == [] and len(fixes) == 1 and "tpm" in fixes[0]  # the plan's default was rpm


def test_the_policy_skill_itself_flags_its_type():
    from orchestration.knowledge import must_ask

    field, question, wordings = must_ask()["policy"]
    assert field == "type" and "rpm" in question and "tpm" in question
    assert "requests per minute" in wordings["rpm"] and "tokens per minute" in wordings["tpm"]


def test_the_evaluation_requests_for_rate_limits_state_their_type_in_plain_words():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))
    from script_cases import SCRIPT_CASES, tool_function

    def never(messages):
        raise AssertionError("judged, though the request states the type plainly")

    for case in SCRIPT_CASES:
        if "policy" in case.expected:
            report = needs.collect(tool_function(case.tool), case.tool, case.expected, case.request, skills=["policy"], ask=never)
            assert [c.key for c in report.choices] == [], case.id
            assert not [f for f in report.fixes if "policy.type" in f], case.id


# ---- defaults among alternatives are shown


def test_a_default_among_sdk_listed_alternatives_is_reported():
    lines = needs.assumed(TOOLS["build_inference_plan"].func, "build_inference_plan", {"inference": {"kubeconfig_path": KUBECONFIG}})
    assert lines == ["inference type `vllm` (alternatives: `llamacpp`, `kserve`)"]


def test_a_type_that_was_given_or_has_no_alternative_is_not_reported():
    func = TOOLS["build_full_stack_plan"].func
    given = {"inference": {"type": "kserve"}, "valkey": {"name": "s"}, "controlplane": {"type": "organization"}}
    assert needs.assumed(func, "build_full_stack_plan", given) == []


def test_a_type_the_user_is_asked_for_is_not_called_an_assumption():
    func = TOOLS["build_full_stack_plan"].func
    assert needs.assumed(func, "build_full_stack_plan", {"policy": {"limits": {"user_default": 1}}}) == []


# ---- the question


ASKING = [
    {"path": "gateway.api_key_secret", "note": ""},
    {"path": "existing_storage_class", "note": "the name of a StorageClass the cluster already has"},
    {"path": "policy.limits", "note": ""},
]


def test_a_question_naming_each_field_or_its_words_asks_for_all_of_them():
    reply = (
        "I need three things: the gateway API key Secret name, which StorageClass your cluster "
        "has, and what limit to apply."
    )
    assert needs.unasked(reply, ASKING) == []
    assert needs.unasked("Please give `gateway.api_key_secret`, `existing_storage_class`, `policy.limits`.", ASKING) == []


def test_a_question_that_leaves_one_out_is_caught():
    assert needs.unasked("Which StorageClass does the cluster have?", ASKING) == [ASKING[0], ASKING[2]]


def test_the_plain_question_lists_every_value_with_its_note():
    text = needs.render(ASKING)
    assert text.splitlines() == [
        "To build this I still need:",
        "- `gateway.api_key_secret`",
        "- `existing_storage_class`: the name of a StorageClass the cluster already has",
        "- `policy.limits`",
    ]


def test_the_instruction_to_the_model_names_exactly_the_values():
    report = collect("build_valkey_plan", {"valkey": {}}, EXISTING)
    message = report.message()
    assert "`valkey.name`" in message and "`valkey.kubeconfig_path`" in message
    assert "Ask the user for exactly these" in message


# ---- storage follows the cluster, and a type with one option is filled in


CHAIN = {"policy": {"type": "rpm"}, "gateway": {}, "valkey": {}, "queue": {}, "controlplane": {"type": "organization"}, "cnpg": {}}


def test_storage_is_not_asked_about_before_the_cluster_is_decided():
    report = collect("build_full_stack_plan", CHAIN, "add rpm rate limiting", skills=["policy"])
    assert [c.key for c in report.choices] == ["cluster"]
    assert "existing_storage_class" not in paths(report.values)
    assert not [fix for fix in report.fixes if "storage" in fix]


def test_an_existing_cluster_named_only_in_words_still_needs_its_storage_class_and_kubeconfig():
    report = collect("build_full_stack_plan", CHAIN, "add rpm rate limiting\nUse an existing cluster", skills=["policy"])
    assert report.fixes == [] and report.choices == []
    assert {"existing_storage_class", "kubeconfig_path"} <= set(paths(report.values))


def test_a_new_cluster_named_only_in_words_composes_storage_and_the_cluster():
    report = collect("build_full_stack_plan", CHAIN, "add rate limiting\nCreate a new cluster", skills=["policy"])
    assert any("add storage" in fix for fix in report.fixes)
    assert any("pass `cluster`" in fix for fix in report.fixes)
    assert "existing_storage_class" not in paths(report.values)
    assert "cluster.name" in paths(report.after_fix)


def test_a_type_with_a_single_supported_value_is_filled_in():
    report = collect("build_storage_plan", {"storage": {"kubeconfig_path": KUBECONFIG}}, EXISTING)
    assert report.args["storage"]["type"] == "longhorn"
    assert "storage.type" not in paths(report.values)


# ---- what a validator demands is a value to give, also when it is hidden
# behind a missing field


def test_a_validator_demanding_a_field_that_was_not_given_is_a_value_to_ask_for():
    report = collect("build_rke2_cluster_plan", {"cluster": {"name": "edge"}}, "Build a new RKE2 cluster called edge.")
    assert report.fixes == []
    assert paths(report.values) == ["cluster.nodes"]
    assert "at least one" in report.values[0].note


def test_a_demand_hidden_behind_a_missing_field_is_asked_for_in_the_same_question():
    # The SDK's validators don't run while a field is missing: `name` alone
    # was reported, and the nodes only after the user had given the name.
    report = collect(
        "build_full_stack_plan", {"cluster": {}, "valkey": {"name": "sessions"}},
        "a valkey cache called sessions\nCreate a new cluster",
    )
    assert report.fixes == [] and report.choices == []
    assert paths(report.values) == ["cluster.name", "cluster.nodes"]


def test_two_values_that_do_not_go_together_stay_a_correction_for_the_model():
    report = collect(
        "build_inference_plan", {"inference": {"kubeconfig_path": KUBECONFIG, "memory_gb": 4}}, EXISTING
    )
    assert report.values == []
    assert len(report.fixes) == 1 and "memory_gb=4 must exceed" in report.fixes[0]


def test_every_ask_case_the_tool_can_detect_is_asked_by_code():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))
    from script_cases import SCRIPT_CASES, tool_function

    by_code, left_to_the_model = [], []
    for case in SCRIPT_CASES:
        report = needs.collect(tool_function(case.tool), case.tool, case.expected, case.request, skills=list(case.layers) + ["policy"])
        if not case.asks:
            assert not report.blocked, (case.id, report.message())
            continue
        if report.asks_user:
            asked = needs.render(report.values).lower()
            assert any(word in asked for word in case.ask_for), (case.id, asked)
            by_code.append(case.id)
        else:
            left_to_the_model.append(case.id)
    # Asking for an encoding the tokenizer doesn't support is a plan the
    # SDK accepts: only the service's own code says no (source_check).
    assert left_to_the_model == ["tokenizer-encoding"]
    assert len(by_code) == 8


# ---- an option the user names that the plan leaves at another default

NODES = [
    {"address": address, "user": "ubuntu", "ssh_key": "~/.ssh/id_ed25519"}
    for address in ("10.0.1.11", "10.0.1.12", "10.0.1.13")
]
THREE_NODES = (
    "Build a new RKE2 cluster called obs with a server node at 10.0.1.11 and agent nodes at 10.0.1.12 "
    "and 10.0.1.13, all reached as user ubuntu with the SSH key ~/.ssh/id_ed25519."
)


def test_nodes_left_without_a_role_while_the_request_names_agents_are_corrected():
    # Live on 2026-10-13 this plan was accepted, and the script built
    # three servers.
    report = collect("build_rke2_cluster_plan", {"cluster": {"name": "obs", "nodes": NODES}}, THREE_NODES)
    assert report.values == [] and report.choices == []
    [fix] = report.fixes
    assert "`role`" in fix and "`agent`" in fix and "'server'" in fix
    assert "3 of the 3" in fix
    assert not report.asks_user


def test_the_same_correction_inside_a_whole_stack_plan():
    args = {"cluster": {"name": "obs", "nodes": NODES}, "valkey": {"name": "sessions"}}
    report = collect("build_full_stack_plan", args, THREE_NODES + " Add a Valkey cache called sessions.")
    assert any("`role`" in fix and "`agent`" in fix for fix in report.fixes)


def test_roles_the_plan_states_are_the_models_decision_and_not_corrected_twice():
    as_asked = [{**NODES[0], "role": "server"}, {**NODES[1], "role": "agent"}, {**NODES[2], "role": "agent"}]
    assert collect("build_rke2_cluster_plan", {"cluster": {"name": "obs", "nodes": as_asked}}, THREE_NODES).fixes == []
    all_servers = [{**node, "role": "server"} for node in NODES]
    assert collect("build_rke2_cluster_plan", {"cluster": {"name": "obs", "nodes": all_servers}}, THREE_NODES).fixes == []


def test_a_default_role_is_fine_when_the_request_names_no_other():
    said = "Build a new RKE2 cluster called obs with nodes at 10.0.1.11, 10.0.1.12 and 10.0.1.13."
    assert collect("build_rke2_cluster_plan", {"cluster": {"name": "obs", "nodes": NODES}}, said).fixes == []
    # "agent" inside another word names nothing.
    said = "Build a new RKE2 cluster called obs for our reagents team, nodes at 10.0.1.11 and 10.0.1.12."
    assert collect("build_rke2_cluster_plan", {"cluster": {"name": "obs", "nodes": NODES[:2]}}, said).fixes == []


def test_a_type_left_at_its_default_while_the_request_names_another_is_corrected():
    said = f"Serve Qwen/Qwen2.5-1.5B-Instruct with KServe. {EXISTING}"
    inference = {"kubeconfig_path": KUBECONFIG, "model": "Qwen/Qwen2.5-1.5B-Instruct"}
    [fix] = collect("build_inference_plan", {"inference": inference}, said).fixes
    assert "`inference.type`" in fix and "`kserve`" in fix and "'vllm'" in fix
    assert collect("build_inference_plan", {"inference": {**inference, "type": "kserve"}}, said).fixes == []
    # The default is what the request names: nothing to correct.
    said = f"Serve Qwen/Qwen2.5-1.5B-Instruct with vLLM. {EXISTING}"
    assert collect("build_inference_plan", {"inference": inference}, said).fixes == []


def test_a_type_the_user_is_asked_for_is_left_to_that_question(policy_type_must_be_asked):
    # The limiter's type is a choice for the user, not a correction.
    report = collect(
        "build_policy_plan",
        {"policy": {"kubeconfig_path": KUBECONFIG, "cache_url": "redis://c:6379", "limits": {"user_default": 5}}},
        f"Limit each user to 5 tokens per minute, tpm. {EXISTING}",
        ask=lambda messages: "[]",
    )
    assert not any("left at its default" in fix for fix in report.fixes)


def test_the_options_a_skill_lists_are_the_ones_the_sdk_accepts():
    # The cluster skill lists a node's roles because the SDK checks them
    # without declaring them. Each has to be accepted, and nothing else.
    from orchestration.knowledge import field_options

    assert field_options()["rke2-cluster"]["role"] == ("server", "agent")
    tool = TOOLS["build_rke2_cluster_plan"].func

    def built(role: str) -> bool:
        nodes = [{"address": "10.0.0.1", "role": "server"}, {"address": "10.0.0.2", "role": role}]
        try:
            return bool(tool(cluster={"name": "x", "nodes": nodes}).get("valid"))
        except ValueError:
            return False

    assert all(built(role) for role in field_options()["rke2-cluster"]["role"])
    assert not built("worker")


def test_no_evaluation_plan_is_corrected_for_an_option_it_already_states():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))
    from fresh_cases import generate
    from script_cases import SCRIPT_CASES, tool_function

    for case in [*SCRIPT_CASES, *generate()]:
        fixes = needs._named_but_unused(tool_function(case.tool), case.tool, case.expected, case.said)
        assert fixes == [], (case.id, fixes)
