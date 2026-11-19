"""How a turn asks the user for something, end to end through the real
graph and the real tools with only the chat model scripted.

What is asked comes from the tool's own findings (orchestration/needs.py).
The model words the question; these tests pin that the wording can't
leave a needed value out, ask for one the system fills in itself, or go
out unchecked, and that an answer to a pause is treated like any new
message from the user."""

from __future__ import annotations

import itertools
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import orchestration.graph as graph
from orchestration import needs

KUBECONFIG = "/etc/rancher/rke2/rke2.yaml"
EXISTING = f"The cluster's kubeconfig is {KUBECONFIG}."
VALKEY_ARGS = {"valkey": {"kubeconfig_path": KUBECONFIG, "name": "sessions"}}
_ids = itertools.count()

# A whole rate-limiting plan on an existing cluster, every value given,
# except which kind of limiter: `policy.type` is left to its default.
RATE_LIMIT = {
    "kubeconfig_path": KUBECONFIG,
    "existing_storage_class": "longhorn",
    "cnpg": {"name": "platform-db", "database": {"name": "appdb", "owner": "appuser", "password": "Kx7-plat-2291"}},
    "valkey": {"name": "platform-cache"},
    "controlplane": {"type": "organization", "existing_secret": "org-cp-secrets"},
    "queue": {},
    "gateway": {"upstream_url": "http://vllm.ai.svc:8000/v1", "api_key_secret": "gateway-keys"},
    "policy": {
        "limits": {"user_default": 60},
        "cache_auth_url": "redis://:Vk81-cache@platform-cache-valkey.valkey.svc:6379/0",
    },
}
RATE_LIMIT_REQUEST = (
    f"Limit each user to 60 per minute. {EXISTING} Its StorageClass is longhorn. Models are served "
    "at http://vllm.ai.svc:8000/v1. Gateway API keys are in the Secret gateway-keys, the "
    "organization control plane's secrets in org-cp-secrets. Postgres cluster platform-db, "
    "database appdb owned by appuser, password Kx7-plat-2291. Cache platform-cache, auth URL "
    "redis://:Vk81-cache@platform-cache-valkey.valkey.svc:6379/0."
)


class ScriptedModel:
    """Replays a fixed list of model replies, one per call_model turn."""

    def __init__(self, replies: list[AIMessage]) -> None:
        self._replies = iter(replies)

    def invoke(self, _messages):
        return next(self._replies)

    async def ainvoke(self, _messages):
        # call_model awaits `ainvoke`, not `invoke` (see orchestration/graph.py).
        return next(self._replies)


def _tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{next(_ids)}"}])


@pytest.fixture
def send(monkeypatch):
    """send(thread_id, message, replies) -- one message on a thread with
    the model scripted. The judgement calls are stubbed: asked which
    option the user stated, the judge says none; anything else gets "no",
    which selection reads as no answer (so the word match selects),
    and the checks on a reply or a plan read as nothing to report."""

    def judge(messages):
        return "[]" if messages[0].content == needs._STATED_INSTRUCTIONS else "no"

    monkeypatch.setattr(graph, "_ask_unbound", judge)
    monkeypatch.setattr(graph, "selection_ambiguity", lambda text: None)
    needs._stated_memo.clear()

    async def _send(thread_id: str, message: str, replies: list[AIMessage]) -> dict:
        model = ScriptedModel(replies)
        monkeypatch.setattr(graph, "_model", lambda _tools=(): model)
        return await graph.send_message(thread_id, message)

    return _send


def _notes(result: dict) -> list[str]:
    return [
        str(m.content) for m in result["messages"]
        if isinstance(m, HumanMessage) and str(m.content).startswith(graph._AGENT_NOTE)
    ]


# ---- values: the model words the question, the content is fixed


async def test_a_question_that_asks_for_every_value_goes_out_as_the_model_wrote_it(send):
    question = "What should the cache be called (`valkey.name`), and where is the kubeconfig (`valkey.kubeconfig_path`)?"
    result = await send(
        uuid.uuid4().hex, f"a valkey cache on my existing cluster",
        [_tool_call("build_valkey_plan", {"valkey": {}}), AIMessage(content=question)],
    )
    assert result["reply"] == question
    assert result["script"] is None and "ask_user" not in result


async def test_a_value_the_model_left_out_of_its_question_is_added(send):
    result = await send(
        uuid.uuid4().hex, "a valkey cache on my existing cluster",
        [_tool_call("build_valkey_plan", {"valkey": {}}), AIMessage(content="What name should the cache have?")],
    )
    assert result["reply"].startswith("What name should the cache have?")
    assert "I also need:" in result["reply"]
    assert "`valkey.kubeconfig_path`: the path to the existing cluster's kubeconfig file" in result["reply"]
    assert "`valkey.name`" not in result["reply"].split("I also need:")[1]


async def test_a_question_asking_for_a_wired_field_is_replaced_by_the_plain_list(send):
    args = {k: v for k, v in RATE_LIMIT.items() if k not in ("policy", "queue", "existing_storage_class")}
    args["controlplane"] = {"type": "organization"}
    result = await send(
        uuid.uuid4().hex, f"Add a model gateway. {EXISTING} Models are at http://vllm.ai.svc:8000/v1.",
        [
            _tool_call("build_full_stack_plan", args),
            AIMessage(content="Please provide the gateway's org_cp_internal_url and `controlplane.existing_secret`."),
        ],
    )
    assert result["reply"] == "To build this I still need:\n- `controlplane.existing_secret`"


async def test_a_question_that_strays_off_topic_is_replaced_by_the_plain_list(send, monkeypatch):
    monkeypatch.setattr(graph.scope_check, "violates", lambda reply, ask: "poem" in reply)
    result = await send(
        uuid.uuid4().hex, "a valkey cache called sessions on my existing cluster",
        [
            _tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}}),
            AIMessage(content="Where is the kubeconfig? Also, here is a poem about caches."),
        ],
    )
    assert result["reply"] == (
        "To build this I still need:\n"
        "- `valkey.kubeconfig_path`: the path to the existing cluster's kubeconfig file"
    )


async def test_ask_user_without_options_is_an_ordinary_question_checked_like_one(send):
    thread = uuid.uuid4().hex
    result = await send(
        thread, "a valkey cache called sessions on my existing cluster",
        [
            _tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}}),
            _tool_call("ask_user", {"question": "Where is the kubeconfig file for that cluster?"}),
        ],
    )
    assert "ask_user" not in result  # not a pause
    assert result["reply"] == "Where is the kubeconfig file for that cluster?"
    assert (await graph._asnapshot(thread)).interrupts == ()


def test_running_out_of_turns_while_a_question_is_pending_still_asks_it():
    asking = [{"path": "valkey.name", "note": ""}]
    assert graph.budget_exhausted({"asking": asking, "script": None})["reply"] == (
        "To build this I still need:\n- `valkey.name`"
    )


async def test_the_model_keeps_calling_instead_of_asking_and_the_user_is_still_asked(send):
    result = await send(
        uuid.uuid4().hex, "a valkey cache called sessions on my existing cluster",
        [_tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}}) for _ in range(graph.MAX_MODEL_TURNS)],
    )
    assert result["reply"].startswith("To build this I still need:")
    assert "`valkey.kubeconfig_path`" in result["reply"]


# ---- a build tool is called before the user is asked anything


async def test_a_question_before_any_tool_call_is_held_back_once(send):
    thread = uuid.uuid4().hex
    result = await send(
        thread, "a valkey cache called sessions",
        [
            AIMessage(content="What is the path to your kubeconfig?"),
            _tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}}),
        ],
    )
    # Held back, the model called the tool, and the tool's own finding --
    # nothing said about the cluster -- was asked as a choice instead.
    assert result["ask_user"]["options"] == [needs.NEW_CLUSTER, needs.EXISTING_CLUSTER]
    snapshot = await graph._asnapshot(thread)
    assert any("call the build tool now" in note for note in _notes(snapshot.values))


async def test_the_same_question_a_second_time_goes_out(send):
    result = await send(
        uuid.uuid4().hex, "a valkey cache called sessions",
        [AIMessage(content="Which cluster is this for?"), AIMessage(content="Which cluster should it go on?")],
    )
    assert result["reply"] == "Which cluster should it go on?"


async def test_an_answer_that_asks_nothing_is_never_held_back(send):
    result = await send(uuid.uuid4().hex, "what is a valkey cache", [AIMessage(content="A Redis-compatible cache.")])
    assert result["reply"] == "A Redis-compatible cache."
    assert _notes(result) == []


# ---- an answer to a pause is a new message from the user


async def test_choosing_a_new_cluster_brings_in_the_cluster_skill_and_the_whole_stack_tool(send):
    thread = uuid.uuid4().hex
    paused = await send(thread, "a valkey cache called sessions", [_tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})])
    assert paused["ask_user"]["options"] == [needs.NEW_CLUSTER, needs.EXISTING_CLUSTER]
    assert "build_full_stack_plan" not in (await graph._asnapshot(thread)).values["tools"]

    plan = {
        "cluster": {"name": "demo", "nodes": [{"address": "10.0.0.60", "user": "root", "role": "server"}]},
        "valkey": {"name": "sessions"},
    }
    built = await send(thread, needs.NEW_CLUSTER, [_tool_call("build_full_stack_plan", plan), AIMessage(content="Here is the plan.")])
    assert "RKE2Cluster(" in built["script"] and "Cache," in built["script"]
    assert "rke2-cluster" in built["skills"] and "build_full_stack_plan" in built["tools"]
    # The model was told which tool the answer calls for.
    assert any("build_full_stack_plan" in note for note in _notes(built))


async def test_choosing_an_existing_cluster_asks_for_its_kubeconfig_in_one_step(send):
    thread = uuid.uuid4().hex
    await send(thread, "a valkey cache called sessions", [_tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})])
    # One model call after the click: the paused call is re-read with the
    # answer, and the model only words the question for what is left.
    resumed = await send(thread, needs.EXISTING_CLUSTER, [AIMessage(content="Where is that cluster's kubeconfig file?")])
    assert resumed["reply"] == "Where is that cluster's kubeconfig file?"
    assert "ask_user" not in resumed


async def test_an_answer_restarts_the_turn_budget(send):
    thread = uuid.uuid4().hex
    paused = await send(
        thread, "a valkey cache called sessions",
        [AIMessage(content=""), _tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})],
    )
    assert "ask_user" in paused
    assert (await graph._asnapshot(thread)).values["attempt"] == 2  # paused with half the budget used

    silent = AIMessage(content="")
    resumed = await send(
        thread, needs.EXISTING_CLUSTER,
        [silent, silent, silent, AIMessage(content="Where is the kubeconfig file?")],
    )
    assert resumed["reply"] == "Where is the kubeconfig file?"
    assert resumed["attempt"] == graph.MAX_MODEL_TURNS


async def test_an_answer_to_the_models_own_question_counts_as_what_the_user_said(send):
    thread = uuid.uuid4().hex
    ask = {"question": "New or existing?", "options": ["new cluster", "existing cluster"]}
    # Offered before any build tool was tried, the choice is held back
    # once; the model offers it again and the turn pauses.
    paused = await send(thread, "a valkey cache called sessions", [_tool_call("ask_user", ask), _tool_call("ask_user", ask)])
    assert paused["ask_user"]["options"] == ["new cluster", "existing cluster"]

    resumed = await send(
        thread, "existing cluster",
        [
            _tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}}),
            AIMessage(content="Where is its kubeconfig file?"),
        ],
    )
    # Not asked new-or-existing a second time by the agent.
    assert "ask_user" not in resumed
    assert resumed["reply"] == "Where is its kubeconfig file?"


# ---- a choice a skill says is the user's


async def test_an_unstated_limiter_type_pauses_with_the_sdks_options_then_builds(send):
    thread = uuid.uuid4().hex
    paused = await send(thread, RATE_LIMIT_REQUEST, [_tool_call("build_full_stack_plan", RATE_LIMIT)])
    assert paused["ask_user"]["options"] == ["rpm", "tpm"]
    assert "requests per minute" in paused["ask_user"]["question"]
    assert paused["script"] is None

    built = await send(thread, "rpm", [_tool_call("build_full_stack_plan", RATE_LIMIT), AIMessage(content="Here is the plan.")])
    assert "ask_user" not in built
    assert "Policy," in built["script"] and "user_default=60" in built["script"]


async def test_a_stated_limiter_type_is_not_asked(send):
    result = await send(
        uuid.uuid4().hex, RATE_LIMIT_REQUEST.replace("60 per minute", "60 rpm"),
        [_tool_call("build_full_stack_plan", RATE_LIMIT), AIMessage(content="Here is the plan.")],
    )
    assert "ask_user" not in result and result["script"]


# ---- the reply for a built plan


async def test_a_default_among_alternatives_is_shown_with_the_plan(send):
    result = await send(
        uuid.uuid4().hex, f"Serve a model. {EXISTING}",
        [_tool_call("build_inference_plan", {"inference": {"kubeconfig_path": KUBECONFIG}}), AIMessage(content="Here is the plan.")],
    )
    prose = result["reply"].split("```python")[0]
    assert prose.startswith("Here is the plan.")
    assert "Assumed, since the request didn't say: inference type `vllm` (alternatives: `llamacpp`, `kserve`)" in prose


async def test_off_topic_wording_about_a_built_plan_falls_back_to_the_plain_summary(send, monkeypatch):
    monkeypatch.setattr(graph.scope_check, "violates", lambda reply, ask: True)
    result = await send(
        uuid.uuid4().hex, f"a valkey cache called sessions. {EXISTING}",
        [_tool_call("build_valkey_plan", VALKEY_ARGS), AIMessage(content="Here it is, and a poem.")],
    )
    assert result["reply"].startswith("Here is the validated deployment script.")
    assert "Sorry" not in result["reply"] and "poem" not in result["reply"]
    assert result["script"] in result["reply"]


async def test_what_the_paused_call_was_building_survives_the_answer(send, monkeypatch):
    # Selection found nothing for the first message; the call the model
    # made still says what the conversation is about.
    monkeypatch.setattr(graph, "select", lambda text, already=(), ask=None: (sorted(already), []))
    thread = uuid.uuid4().hex
    await send(thread, "one of those please", [_tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})])
    resumed = await send(thread, needs.NEW_CLUSTER, [AIMessage(content="A statement.")])
    assert resumed["skills"] == ["rke2-cluster", "valkey"]


# ---- the request that used to take four rounds of questions


async def test_a_vague_request_gets_its_choices_then_one_complete_question(send):
    """ "can you add rate limiting on the API for me": before, the limit,
    the dependency chain, the storage class, the names and secrets and
    the cache password each came out one round at a time, with four
    model turns to do it in."""
    thread = uuid.uuid4().hex
    chain = {
        "policy": {}, "gateway": {}, "valkey": {}, "queue": {},
        "controlplane": {"type": "organization"}, "cnpg": {},
    }
    first = await send(
        thread, "can you add rate limiting on the API for me",
        [_tool_call("build_full_stack_plan", {"policy": {}}), _tool_call("build_full_stack_plan", chain)],
    )
    assert first["ask_user"]["options"] == [needs.NEW_CLUSTER, needs.EXISTING_CLUSTER]

    second = await send(thread, needs.EXISTING_CLUSTER, [])  # no model call: the next choice is already known
    assert second["ask_user"]["options"] == ["rpm", "tpm"]

    third = await send(thread, "rpm", [AIMessage(content="I need a few values to finish this.")])
    assert "ask_user" not in third and third["script"] is None
    assert third["reply"].startswith("I need a few values to finish this.")
    for value in (
        "`policy.limits`", "`existing_storage_class`", "`kubeconfig_path`", "`gateway.upstream_url`",
        "`gateway.api_key_secret`", "`valkey.name`", "`controlplane.existing_secret`",
        "`policy.cache_auth_url`", "`cnpg.database`",
    ):
        assert value in third["reply"], value
    for wired in ("cache_url`", "event_backbone_url", "org_cp_internal_url"):
        assert wired not in third["reply"], wired


async def test_a_choice_offered_before_any_tool_call_is_held_back_once(send):
    thread = uuid.uuid4().hex
    ask = {"question": "New or existing?", "options": ["yes, a new one", "no, I have one"]}
    paused = await send(
        thread, "a valkey cache called sessions",
        [_tool_call("ask_user", ask), _tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})],
    )
    # Held back, the model tried the tool, and the cluster question went
    # out in the agent's own wording, whose answers it recognizes.
    assert paused["ask_user"]["options"] == [needs.NEW_CLUSTER, needs.EXISTING_CLUSTER]
    assert graph._NOT_ASKED not in graph._user_text((await graph._asnapshot(thread)).values)


async def test_a_question_instead_of_a_choice_is_answered_not_asked_again(send):
    thread = uuid.uuid4().hex
    await send(thread, "a valkey cache called sessions", [_tool_call("build_valkey_plan", {"valkey": {"name": "sessions"}})])
    explanation = "A new cluster is built on your nodes; an existing one needs its kubeconfig. Which would you like?"
    resumed = await send(thread, "what is the difference?", [AIMessage(content=explanation)])
    assert "ask_user" not in resumed  # not the same choice straight back
    assert resumed["reply"] == explanation


async def test_ask_user_with_a_single_option_is_an_ordinary_question_too(send):
    # Seen live: after the tokenizer's source said only cl100k_base is
    # supported, the model "offered a choice" of that one encoding.
    thread = uuid.uuid4().hex
    question = "The tokenizer only supports cl100k_base. Shall I use that?"
    result = await send(
        thread, "a valkey cache called sessions on my existing cluster",
        [
            _tool_call("build_valkey_plan", {"valkey": {"name": "sessions", "kubeconfig_path": KUBECONFIG, "no_such": 1}}),
            _tool_call("ask_user", {"question": question, "options": ["cl100k_base"]}),
        ],
    )
    assert "ask_user" not in result and result["reply"] == question
    assert (await graph._asnapshot(thread)).interrupts == ()
