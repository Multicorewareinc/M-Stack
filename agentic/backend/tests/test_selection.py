"""Tests for orchestration/selection.py -- which skills and tools a given
request pulls in.

The thing being protected here is a hard limit, not a preference: loading
every skill and binding every tool costs ~48k tokens, and the model this
layer runs against has a 32,768-token window, so the unselected prompt
could not be sent at all.
"""

import pytest

from mcp_tools.architecture import DEPENDS_ON, SKILL_TO_PARAM
from orchestration.knowledge import build_system_prompt, load_skills
from orchestration.selection import (
    PROMPT_BUDGET_TOKENS,
    TOOL_FOR_SKILL,
    WHOLE_STACK_TOOL,
    _estimate,
    select,
    select_skills,
    triggers,
)

ALL_SKILLS = [n for n, _ in load_skills()]



@pytest.fixture(autouse=True)
def _default_budget(monkeypatch):
    """These pin behaviour at the default budget. A .env that raises
    LLM_PROMPT_BUDGET_TOKENS reaches the test process through
    load_dotenv(), so it is cleared here."""
    monkeypatch.delenv("LLM_PROMPT_BUDGET_TOKENS", raising=False)


@pytest.mark.parametrize(
    "request_text, expected",
    [
        ("Set up an RKE2 cluster with a server node at 10.0.0.11", "rke2-cluster"),
        ("Add Longhorn block storage with 2 replicas", "storage"),
        ("Set up a MinIO tenant for model weights", "minio"),
        ("Deploy vLLM serving Qwen2.5-7B", "inference"),
        ("Add a model gateway in front of it", "gateway"),
        ("Add rate limiting, 60 requests per minute", "policy"),
        ("Add an ingress with MetalLB and Istio", "ingress-gateway"),
        ("Add a Valkey cache", "valkey"),
        ("Create a Postgres database with CloudNativePG", "cnpg"),
        ("Set up Prometheus and Grafana dashboards", "observability"),
        ("Add a tokenizer for token counting", "tokenizer"),
        ("Deploy the organization control plane", "controlplane"),
        ("Deploy the admin portal UI", "portal"),
    ],
)
def test_each_capability_is_found_by_its_natural_phrasing(request_text, expected):
    assert expected in select_skills(request_text)


def test_every_skill_on_disk_is_reachable():
    """A skill nothing can select is a skill the model never sees. Guards
    against a new SKILL.md whose description doesn't follow the
    'Use this skill when the user's request mentions ...' form the
    trigger parser reads."""
    unreachable = [name for name, terms in triggers().items() if not terms]
    assert not unreachable


def test_every_skill_has_a_tool():
    assert set(TOOL_FOR_SKILL) == set(ALL_SKILLS)


def test_a_plain_cluster_request_does_not_drag_in_the_ingress_skill():
    """Regression: the ingress-gateway description mentions "how the
    cluster is reached from outside", which made "set up a cluster" select
    it -- and, with two skills matched, bind the whole-stack tool's ~11k
    schema for a request about neither."""
    skills, tools = select("Set up an RKE2 cluster called demo with a server node at 10.0.0.11")
    assert skills == ["rke2-cluster"]
    assert tools == ["build_rke2_cluster_plan"]


def test_a_policy_request_does_not_drag_in_the_gateway_skill():
    # The policy description mentions "a policy chain in front of a model
    # gateway" -- context, not a trigger.
    assert "gateway" not in select_skills("Add rate limiting, 60 requests per minute per user")


def test_shared_terms_that_no_skill_owns_still_select_both():
    # "admin" belongs to no directory name, and an admin control plane and
    # admin portal really are deployed as a pair.
    skills = select_skills("Deploy the admin control plane and the admin portal")
    assert "controlplane" in skills and "portal" in skills


def test_whole_stack_tool_is_bound_for_a_whole_platform_request():
    _skills, tools = select("Set up an entire platform: a cluster, storage and MinIO")
    assert WHOLE_STACK_TOOL in tools


def test_whole_stack_tool_is_not_bound_for_a_single_resource_request():
    _skills, tools = select("Add a Valkey cache called platform-cache")
    assert WHOLE_STACK_TOOL not in tools


def test_conversational_message_selects_nothing():
    skills, tools = select("what can you do")
    assert skills == [] and tools == []


def test_selection_accumulates_across_turns():
    """A follow-up that names no resource must keep the thread's existing
    selection -- otherwise "set its replica count to 2" arrives with no
    skill and no tool."""
    skills, tools = select("set its replica count to 2", already=["valkey"])
    assert "valkey" in skills
    assert "build_valkey_plan" in tools


@pytest.mark.parametrize(
    "request_text",
    [
        "Set up an RKE2 cluster with a server node at 10.0.0.11",
        "Set up an entire platform: a new cluster, block storage, a MinIO tenant and inference",
        "build everything: rke2 cluster, longhorn storage, minio, vllm inference, model "
        "gateway, rate limiting policy, ingress gateway metallb, valkey cache, cnpg "
        "postgres, prometheus monitoring, tokenizer, control plane and portal",
    ],
)
def test_selection_never_exceeds_the_prompt_budget(request_text):
    skills, tools = select(request_text)
    assert _estimate(skills, tools) <= PROMPT_BUDGET_TOKENS


def test_naming_every_resource_degrades_instead_of_overflowing():
    """The pathological case: a request that genuinely selects everything.

    It must come back under budget while still carrying real skills and
    the whole-stack tool. It must not collapse to the whole-stack tool
    alone either: the components with no dependencies keep their own
    tools, for adding one of them to an existing cluster."""
    text = ("build everything: rke2 cluster, longhorn storage, minio, vllm inference, model "
            "gateway, rate limiting policy, ingress gateway metallb, valkey cache, cnpg "
            "postgres, prometheus monitoring, tokenizer, control plane and portal")
    skills, tools = select(text)
    assert _estimate(skills, tools) <= PROMPT_BUDGET_TOKENS
    assert skills, "should not have dropped every skill"
    assert WHOLE_STACK_TOOL in tools, "this request really is a whole-platform build"
    assert [t for t in tools if t != WHOLE_STACK_TOOL], "dependency-free tools must survive"
    for skill in skills:
        if DEPENDS_ON.get(SKILL_TO_PARAM.get(skill, skill)):
            assert TOOL_FOR_SKILL[skill] not in tools, f"{skill} must only be built with its chain"
        else:
            assert TOOL_FOR_SKILL[skill] in tools


def test_unselected_prompt_really_would_overflow():
    """The premise of this whole module, asserted rather than assumed: if
    this ever stops being true the budget can be revisited."""
    everything = _estimate(ALL_SKILLS, list(TOOL_FOR_SKILL.values()) + [WHOLE_STACK_TOOL])
    assert everything > 32_768


def test_build_system_prompt_filters_to_the_named_skills():
    prompt = build_system_prompt(["cnpg", "valkey"])
    assert "--- cnpg ---" in prompt and "--- valkey ---" in prompt
    assert "--- gateway ---" not in prompt


def test_build_system_prompt_with_no_argument_still_loads_everything():
    prompt = build_system_prompt()
    for name in ALL_SKILLS:
        assert f"--- {name} ---" in prompt


def test_a_dependency_free_component_on_an_existing_cluster_gets_only_its_own_tool():
    """Regression, found by a live run: offered the whole-stack tool for
    "I already have a cluster, add X", the model built a second cluster.
    For a component with no dependencies its own tool is the whole job."""
    for text in [
        "I already have a cluster with kubeconfig at /k.yaml. Add a Valkey cache called sessions on it.",
        "On that same cluster, add longhorn storage.",
        "I have a cluster with kubeconfig at /k.yaml. Deploy a CNPG postgres database on it.",
    ]:
        _skills, tools = select(text)
        assert WHOLE_STACK_TOOL not in tools, text


def test_a_dependent_component_on_an_existing_cluster_gets_its_whole_chain():
    """"Add a gateway to my existing cluster" means the gateway plus its
    control plane, Postgres and Redis. The whole-stack tool now builds onto
    an existing cluster through kubeconfig_path, so it is offered -- and
    test_mcp_tools_full_stack's existing-cluster tests confirm that path
    never provisions a cluster."""
    for text in [
        "I already have a cluster with kubeconfig at /home/me/kubeconfig.yaml. "
        "Add a model gateway on it proxying to http://vllm:8000 with keys in gateway-keys.",
        "On that same cluster, set up a MinIO tenant called store with 4 servers.",
        "I have a cluster with kubeconfig at /k.yaml. Deploy the organization control plane on it.",
    ]:
        skills, tools = select(text)
        assert WHOLE_STACK_TOOL in tools, text


def test_a_dependent_component_is_never_offered_its_lone_single_resource_tool():
    """The tool that builds a gateway alone is how a request for one used
    to come back without the control plane it verifies every key against."""
    text = (
        "I already have a cluster with kubeconfig at /home/me/kubeconfig.yaml. "
        "Add a model gateway on it proxying to http://vllm:8000 with keys in gateway-keys."
    )
    _skills, tools = select(text)
    assert "build_gateway_plan" not in tools
    assert WHOLE_STACK_TOOL in tools
    # With room for them (a large-context model), the rules for the whole
    # chain load too. At the default budget they are the first thing
    # trimmed; the whole-stack tool's own dependency checks still hold.
    skills, _tools = select(text, budget=100_000)
    assert {"gateway", "controlplane", "cnpg", "valkey"} <= set(skills)


def test_a_tokenizer_request_brings_its_whole_chain():
    """"build a tokenizer" -- the request that returned a lone tokenizer."""
    skills, tools = select("build a tokenizer for me")
    assert WHOLE_STACK_TOOL in tools
    assert "build_tokenizer_plan" not in tools
    assert "enricher" in skills


def test_trimming_never_drops_the_whole_stack_tool_a_dependent_component_needs():
    """Dropping it to save budget would leave only single-resource tools,
    and a component built without the chain it depends on."""
    everything = [
        "gateway", "inference", "ingress-gateway", "policy", "rke2-cluster",
        "tokenizer", "valkey", "minio", "storage", "cnpg",
    ]
    skills, tools = select(
        "On that same cluster, add a MinIO tenant called store with 4 servers",
        already=everything,
    )
    assert _estimate(skills, tools) <= PROMPT_BUDGET_TOKENS
    assert WHOLE_STACK_TOOL in tools
    assert "build_minio_plan" not in tools


def test_carried_context_is_shed_before_the_current_requests_own_skills():
    """What actually exhausted the budget in the live run was four turns of
    accumulated skills, not any one request. The current message's own
    matches must outlive the carried ones."""
    skills, _tools = select(
        "add a MinIO tenant called store with 4 servers",
        already=["gateway", "inference", "ingress-gateway", "policy",
                 "rke2-cluster", "tokenizer", "valkey", "cnpg", "storage"],
    )
    assert "minio" in skills


def test_an_explicit_whole_platform_request_still_gets_the_whole_stack_tool():
    # The suppression above must not fire when the user really is building
    # from nothing, even though "everything" is also an existing-ish word.
    _skills, tools = select(
        "Set up an entire platform: a new cluster called demo with a server "
        "node at 10.0.0.60, a Valkey cache and a tokenizer"
    )
    assert WHOLE_STACK_TOOL in tools


def test_a_database_or_cache_cluster_is_not_an_rke2_cluster():
    """"postgres cluster org-db" loaded the cluster-building rules
    mid-conversation, and the model re-asked where to install."""
    for text in ["postgres cluster org-db, database org", "a valkey cluster called cache", "the nats cluster"]:
        assert "rke2-cluster" not in select_skills(text), text
    assert "rke2-cluster" in select_skills("set up an rke2 cluster with a server node")
    assert "rke2-cluster" in select_skills("a new cluster with storage")


def test_gateway_means_the_model_gateway_and_ingress_gateway_is_one_thing():
    """"gateway keys are in gateway-keys" loaded the ingress rules, and the
    model asked for a MetalLB address pool nothing in the plan needed."""
    assert select_skills("gateway keys are in gateway-keys") == ["gateway"]
    for text in ["set up an ingress gateway with metallb", "the ingress-gateway", "expose it through istio"]:
        assert select_skills(text) == ["ingress-gateway"], text


def test_how_is_not_a_trigger():
    assert select_skills("how does billing work") == ["billing"]
