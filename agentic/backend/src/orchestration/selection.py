"""Choosing which skills and tools a single request actually needs.

Loading all thirteen SKILL.md files and binding all fourteen tool schemas
on every request costs roughly 48,000 tokens before the user has typed
anything -- more than the 32,768-token context of the model this layer is
configured against, so every request overflowed. That cost also grew with
each capability added, so it was going to keep getting worse.

This is the selection step `docs/architecture-decision.md` calls Phase C.
Which skills a request needs is decided by the chat model itself, in one
structured call (`select_skills()`): the prompt lists all 19 capabilities'
own trigger descriptions, and the model answers which ones the request is
actually asking to build or configure -- not merely mentioning, or
describing a DIFFERENT component's behavior toward ("let requests through
if the rate-limit service can't be reached" is about the gateway's own
policy_fail_mode field, not a request for a rate limiter; see
gateway/SKILL.md and policy/SKILL.md's own documented rule). Nineteen
short descriptions is cheap context -- there is no need to narrow them
first with a separate retrieval pass; an earlier two-stage version of
this (embedding recall, then a confirmation call) was replaced once that
turned out to just be two calls doing one job. Each component's listing
also carries a rough RAG meaning-match score now (see
_llm_select_skills()), as extra evidence for the model to weigh -- a
second attempt at letting RAG decide this on its own (comparing that
same score to a fixed line, no judgement call at all) was tried and
reverted after a real request measured it failing: two components
sharing almost all their vocabulary (anything deployed onto a cluster
via a kubeconfig file, which is every component here) scored within 0.02
of each other regardless of which one the request actually asked for.
Reading the sentence is still what decides; RAG only widens what it
reads.

This is what a production system asks a model to do, not an ever-growing
exception list: a maintained word list has a hard ceiling no amount of
patching removes, since it can only guard against a false match someone
has already seen, never the next one -- confirmed directly: embedding
similarity alone also could not make this call ("let requests through if
the rate-limit service can't be reached" scored *higher* against the
policy skill, 0.734, than a genuine rate-limiting request did, 0.694,
because both are genuinely about the same topic; a word list fares no
better, since both sentences contain the same trigger words). Only a real
judgement tells a request FOR something from a request merely ABOUT it.

Literal trigger-word matching (`_word_match_skills`) still exists, but
only as a degraded-mode safety net for when no model call is available
this turn, so a request is never left with no selection at all just
because the call failed. It is not the primary mechanism.

Triggers are not maintained here, for either path. Every SKILL.md already
declares its own in frontmatter -- "Use this skill when the user's request
mentions Postgres, PostgreSQL, CloudNativePG, CNPG, or a database to
deploy" -- and that sentence is both the fallback's word list and what the
model reads directly. A new capability therefore needs nothing added to
this module.

Selection is deliberately generous. Including a skill the request did not
need costs tokens; missing one costs the model the rules it needs to avoid
guessing a value, which is the failure this whole layer exists to prevent.
So a partial match includes the skill, and anything resembling a
whole-platform request pulls in the composed tool as well.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from langchain_core.messages import HumanMessage, SystemMessage

from mcp_tools.architecture import DEPENDS_ON, PARAM_TO_SKILL, SKILL_TO_PARAM
from mcp_tools.full_stack import model_server_given
from orchestration import grounding
from orchestration.knowledge import load_skills

logger = logging.getLogger(__name__)

# skills/<dir> -> the tool that builds that resource.
TOOL_FOR_SKILL: Dict[str, str] = {
    "rke2-cluster": "build_rke2_cluster_plan",
    "storage": "build_storage_plan",
    "seaweedfs": "build_seaweedfs_plan",
    "inference": "build_inference_plan",
    "gateway": "build_gateway_plan",
    "policy": "build_policy_plan",
    "ingress-gateway": "build_ingress_gateway_plan",
    "valkey": "build_valkey_plan",
    "cnpg": "build_cnpg_plan",
    "observability": "build_observability_plan",
    "tokenizer": "build_tokenizer_plan",
    "controlplane": "build_controlplane_plan",
    "portal": "build_portal_plan",
    "enricher": "build_enricher_plan",
    "billing": "build_billing_plan",
    "route": "build_route_plan",
    "accelerator": "build_accelerator_plan",
    "queue": "build_queue_plan",
    "vault": "build_vault_plan",
}

WHOLE_STACK_TOOL = "build_full_stack_plan"

# Phrases that mean "build several layers together from nothing", which is
# what build_full_stack_plan is for. Its schema alone is ~11k tokens -- the
# single largest item in the prompt -- so it is bound only when the request
# actually looks like one, or when enough separate resources were named
# that the model would otherwise have to chain single-resource tools.
_WHOLE_STACK_HINTS = (
    "whole stack", "full stack", "entire platform", "whole platform",
    "complete platform", "entire stack", "complete stack", "everything",
    "end to end", "end-to-end", "from scratch", "all of it",
)

# Phrases that say the cluster already exists. For a component with no
# dependencies these suppress the whole-stack tool: its own tool is the
# whole job, and the whole-stack tool once turned "add storage to my
# existing cluster" into a script that provisioned a second cluster. A
# component that does have dependencies still gets the whole-stack tool,
# which builds onto an existing cluster through its kubeconfig_path.
_EXISTING_HINTS = (
    "already have", "already has", "already got", "existing cluster",
    "i have a cluster", "we have a cluster", "same cluster", "that cluster",
    "on it", "add to", "existing infrastructure", "already running",
    "already deployed", "already exists",
)

# Words too generic to identify a capability on their own. Without this,
# "model" alone would pull in four skills and "api" nearly all of them.
# Words of two letters or fewer carry no signal, except these: "the admin
# UI" names a portal as surely as "the admin portal" does.
_SHORT_TERMS = frozenset({"ui"})

_STOPWORDS = {
    "a", "an", "the", "or", "and", "to", "for", "of", "in", "on", "with",
    "deploy", "deployment", "request", "requests", "mentions", "user",
    "skill", "use", "this", "when", "real", "covers", "classes", "sdk",
    "multistack", "what", "should", "its", "own", "other", "from",
    "model", "models", "api", "service", "services", "front", "door",
    "store", "storing", "compute", "counting", "accounting", "responses",
    "chain", "plans", "permissions", "administration", "quotas",
    "provisioning", "reached", "outside", "how",
}
# NB: "cluster" is deliberately NOT a stopword. It reads like one -- half
# the descriptions mention it -- but it is the rke2-cluster skill's
# primary trigger, and stopwording it meant "set up an entire platform: a
# new cluster, storage and MinIO" loaded no cluster skill at all, so the
# model had RKE2Node's rules nowhere while being asked to build nodes.

# Real sentences that score deceptively high against a skill by topic
# alone, without actually requesting it -- each confirmed by an actual
# false match, not guessed. Used two ways:
#
# 1. The single words inside them (_NOT_ALONE, below) stop the
#    trigger-word fallback matcher from firing standalone -- "rate" alone
#    would catch "failure rate"; these words still count towards their
#    skill's own multi-word phrases ("rate limiting", "block storage",
#    "token counting", ...), only the bare single-word trigger is dropped.
#
# 2. The full sentences are indexed as negative examples for RAG-based
#    selection (corpus.py's skill_counterexample_chunks()) and compared
#    against a request alongside the skill's own positive trigger
#    description -- raw similarity alone cannot tell "about" from "for":
#    "build a tokenizer that counts tokens" scored high against policy
#    purely on topic overlap (both genuinely about limits/counting),
#    despite never asking for one. Subtracting the best-matching
#    counterexample's own score is what separates these -- see
#    service.py's rank_skills().
#
#    NOT covered here, on purpose: the gateway's own fail-open wording
#    ("let requests through if the rate-limit service can't be reached,"
#    about policy_fail_mode, not a request for Policy -- see
#    gateway/SKILL.md and policy/SKILL.md's own documented rule). Tested
#    live against the real index: it scored 0.6174 against policy, still
#    above a genuine rate-limiting request's 0.5919 -- the counterexample
#    subtraction narrows the gap but does not flip it for this specific
#    pair, so a FALSE_MATCHES entry for it would be a false sense of
#    safety, not a real fix. Deliberately left unhandled here -- not a
#    target for this mechanism; see the human-in-the-loop plan for how
#    genuinely ambiguous cases like this one are meant to be handled
#    instead.
FALSE_MATCHES: Dict[str, List[str]] = {
    "policy": [
        "failure rate",
        "error rate",
        "build a tokenizer that counts tokens",  # in the recorded test set
        # (tokenizer-encoding) -- was pulling in policy's whole dependency
        # chain for a request that never mentioned limits
        "deploy 3 replicas per node",
        "give it a minute to warm up",
        "replace the old flow limiter",
    ],
    "route": [
        "kubeconfig_path",  # "path" matches inside it (an underscore isn't
        # a word character), so nearly every request loaded the route skill
        "the network traffic looks fine",
    ],
    "tokenizer": ["store the auth token in a secret"],
    "enricher": [
        "store the auth token in a secret",
        "count the nodes in the cluster",
        "replace the old flow limiter",
    ],
    "storage": ["block traffic from the old subnet"],
    "gateway": ["give me the SSH keys for the new nodes"],
}
# None of the words below appears bare (without a stronger signal already
# present, like the skill's own identity word) in any of this project's 40
# recorded script-generation test cases, so removing them as standalone
# triggers costs no known legitimate match.
_NOT_ALONE = frozenset(
    {"rate", "path", "token", "tokens", "block", "keys", "count", "limiter", "traffic", "per", "minute"}
)

_MENTIONS = re.compile(r"mentions\s+(.*?)(?:\.\s|\.$)", re.IGNORECASE | re.DOTALL)
_SPLIT = re.compile(r",|\bor\b", re.IGNORECASE)


def _description(skill_md: str) -> str:
    """The `description:` line from a SKILL.md's frontmatter."""
    for line in skill_md.splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def _phrases(description: str) -> Set[str]:
    """Trigger phrases a SKILL.md's own description declares.

    Keeps each comma-separated phrase whole ("rate limiting", "control
    plane") and also its individually meaningful words, so both "add rate
    limiting" and "limit requests per minute" reach the policy skill.
    """
    match = _MENTIONS.search(description)
    if not match:
        return set()

    found: Set[str] = set()
    for chunk in _SPLIT.split(match.group(1)):
        phrase = chunk.strip().strip(".").lower()
        # Leading articles carry no signal and would never match anyway.
        phrase = re.sub(r"^(a|an|the)\s+", "", phrase)
        if not phrase:
            continue
        words = [w for w in re.findall(r"[a-z0-9][a-z0-9./-]*", phrase)]
        meaningful = [
            w for w in words if (len(w) > 2 or w in _SHORT_TERMS) and w not in _STOPWORDS
        ]
        # The whole phrase, when it is more than one useful word.
        if len(meaningful) > 1:
            found.add(" ".join(words))
        found.update(w for w in meaningful if w not in _NOT_ALONE)
    return found


def _identity(name: str) -> Set[str]:
    """The words that ARE this skill, from its directory name."""
    return {w for w in name.lower().split("-") if len(w) > 2}


def triggers() -> Dict[str, Set[str]]:
    """skills/<dir> -> the trigger terms that skill declares for itself.

    A description often names another capability to give context -- the
    ingress-gateway's says "how the cluster is reached from outside", the
    policy's "a policy chain in front of a model gateway". Taken literally
    those made "set up a cluster" load the ingress skill, and with it the
    whole-stack tool's 11k-token schema, for a request about neither.

    So a bare term that is another skill's own identity (a word of its
    directory name) belongs to that skill alone: "cluster" selects
    rke2-cluster, "storage" selects storage, "gateway" selects the two
    gateways and not policy. Multi-word phrases are untouched, so seaweedfs
    still answers to "object storage", and terms no directory claims keep
    selecting every skill that named them. The portal names none of
    "admin" and "organization": every gateway request mentions the
    organization control plane, and with the portal notes loaded the model
    built a portal nobody asked for in 9 of 105 evaluation runs.
    """
    parsed = {name: (_phrases(_description(content)), _identity(name))
              for name, content in load_skills()}
    # A word that is another skill's whole name belongs to that skill:
    # "gateway" is the model gateway, not ingress-gateway too. Shared, a
    # message about "gateway keys" loaded the ingress rules, and the model
    # asked for a MetalLB address pool nothing in the plan needed.
    for name, (terms, ident) in parsed.items():
        parsed[name] = (terms, ident - (set(parsed) - {name}))
    owned: Set[str] = set().union(*(ident for _, ident in parsed.values()))

    built: Dict[str, Set[str]] = {}
    for name, (terms, ident) in parsed.items():
        terms = {t for t in terms if t not in owned or t in ident}
        # The skill's own directory name is always a trigger ("cnpg",
        # "portal"), including the unhyphenated form of a hyphenated one.
        terms |= ident
        terms.add(name.lower())
        if "-" in name:
            terms.add(name.replace("-", " ").lower())
            terms.add(name.replace("-", "").lower())
        built[name] = terms
    return built


def _matches(text: str, term: str) -> bool:
    """Whole-word match, so "s3" doesn't hit inside "s3x" and "cni" doesn't
    match inside a longer word."""
    return _first(text, term) is not None


def _first(text: str, term: str) -> int | None:
    """Where `term` first occurs in `text` as a whole word, or None."""
    found = re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text)
    return found.start() if found else None


# "cluster" after a database, cache or queue word names that component's own
# cluster -- "postgres cluster org-db" -- not an RKE2 one. Matched as the
# rke2-cluster trigger it loaded the cluster-building rules mid-conversation,
# and the model asked "new cluster or existing?" after the user had said.
_NOT_AN_RKE2_CLUSTER = re.compile(
    r"\b(postgres(?:ql)?|cnpg|database|db|valkey|redis|cache|nats|jetstream|vault|raft|seaweedfs)\s+cluster\b"
)


# "ingress gateway" is one component; read word by word its "gateway" also
# selected the model gateway, and with it that gateway's whole dependency
# chain, for a request about the front door alone.
_INGRESS_GATEWAY = re.compile(r"\bingress[\s-]+gateway\b")


def _normalise(text: str) -> str:
    text = _NOT_AN_RKE2_CLUSTER.sub(r"\1", text.lower())
    return _INGRESS_GATEWAY.sub("ingressgateway", text)


def _word_match_skills(text: str) -> List[str]:
    """select_skills()'s fallback: skill names relevant to `text` by
    literal trigger-word matching. Used only when no model call is
    available this turn -- see the module docstring."""
    lowered = _normalise(text)
    return sorted(
        name for name, terms in triggers().items()
        if any(_matches(lowered, term) for term in terms)
    )


_SELECT_INSTRUCTIONS = (
    "You decide which of the platform's components a deployment request is actually asking to "
    "build, deploy or configure. Confirm a component only when the request really wants it -- not "
    "when it is merely mentioned, named while describing a DIFFERENT component's own behavior "
    "(what a gateway does if a rate limiter is unreachable is about the gateway's own "
    "policy_fail_mode field, not a request for a rate limiter -- a gateway can set it with no "
    "Policy deployed at all), or a similar word is used for something unrelated. The components "
    "are listed with a rough meaning-match score out of 1.0 -- read it as a hint about which ones "
    "share vocabulary with the request, nothing more: two components can share almost every word "
    "(both are things deployed onto a cluster via a kubeconfig file) while only one is actually "
    "being asked for, so a high score is not confirmation and a low one is not exclusion. Decide "
    "from the request's actual wording, not the score. Reply with JSON only: a list of the "
    "confirmed components' names, exactly as given below. Reply [] if none of them are actually "
    "being requested."
)
_ARRAY = re.compile(r"\[.*\]", re.S)


def _llm_select_skills(text: str, ask: Optional[Callable]) -> List[str] | None:
    """Every component `text` is actually asking to build or configure, by
    one structured judgement call over all of the platform's own trigger
    descriptions (see the module docstring for why this isn't a two-step
    recall-then-confirm) -- this is the actual decision; RAG never makes
    it. Same call pattern as source_check.py's verify(): JSON reply,
    dropped rather than trusted if it doesn't parse as the list of names
    it was asked for. None when `ask` is None or the call fails, so the
    caller falls back to _word_match_skills().

    RAG's embedding match (orchestration/grounding.py's relevant_skills())
    is folded into the prompt as a rough score beside each component, not
    used to filter or decide anything itself -- the model reads it as one
    more piece of evidence alongside the real description and the
    request's own wording. This is deliberate: a pure-score selection
    was tried and measured to fail on a real
    case: "Add a valkey cache called sessions. Cluster kubeconfig is
    .../rke2.yaml" scored valkey at 0.63 and rke2-cluster at 0.61 --
    nearly every component shares the same cluster/kubeconfig vocabulary,
    so the score alone cannot separate "the thing being requested" from
    "a thing that happens to share vocabulary with the request." Reading
    the actual sentence can, which is why a real judgement call decides,
    not a threshold."""
    if ask is None:
        return None
    descriptions = {name: _description(content) for name, content in load_skills()}
    scores = dict(grounding.relevant_skills(text))

    def _line(name: str, desc: str) -> str:
        hint = f" (meaning-match {scores[name]:.2f})" if name in scores else ""
        return f"- {name}{hint}: {desc}"

    listed = "\n".join(_line(name, desc) for name, desc in sorted(descriptions.items()))
    question = f'Request: "{text}"\n\nComponents:\n{listed}'
    try:
        answer = ask([SystemMessage(content=_SELECT_INSTRUCTIONS), HumanMessage(content=question)])
        match = _ARRAY.search(answer or "")
        picked = json.loads(match.group(0)) if match else None
    except Exception as e:  # never worse off than falling back to word matching
        logger.warning("selection: model-based selection skipped: %s", e)
        return None
    if not isinstance(picked, list):
        return None
    return sorted({p for p in picked if p in descriptions})


# Below this score, a skill's match to the request is treated as noise,
# not a real pick -- rank_skills() ranks every indexed skill against every
# request, never returning "none of them," so *some* cutoff is required.
#
# What rank_skills() returns is a MARGIN, not a raw similarity, as of the
# fix described in _rag_select_skills()'s own docstring below: a skill's
# raw score minus its best-matching known false-match example's score
# (0 subtracted for a skill with no false-match examples indexed). A
# margin runs smaller than a raw score by construction, so this is NOT
# the same number space the first version of this threshold (0.6,
# calibrated against raw scores alone) was picked in -- that number would
# now reject genuine matches for every skill FALSE_MATCHES covers. Lowered
# accordingly, but still a placeholder, not a measurement: neither value
# has been calibrated against real requests the way the module docstring's
# own 0.734-vs-0.694 pair was measured for the embedding-vs-judgement
# comparison. Tune it against tests/eval/'s request corpus before relying
# on it -- see the rollout plan's "Threshold calibration" step.
SKILL_MATCH_THRESHOLD = float(os.environ.get("SKILL_MATCH_THRESHOLD", "0.2"))


def _rag_select_skills(text: str) -> List[str] | None:
    """Every component `text` is actually asking to build or configure, by
    embedding `text` and ranking it against every skill's own trigger
    description (orchestration/grounding.py's relevant_skills(), RAG
    service's rank_skills() / /v1/relevant-skills). A skill is kept when
    its margin clears SKILL_MATCH_THRESHOLD. None when RAG is off or the
    call fails, so the caller falls back to _word_match_skills() -- same
    safety net _llm_select_skills() used for a failed model call.

    NOT CURRENTLY USED by select_skills()'s live path. This was tried as
    the live path first, then reverted after being measured to fail on a
    real request: "Add a
    valkey cache called sessions. Cluster kubeconfig is .../rke2.yaml"
    scored valkey (the right answer) at 0.63 and rke2-cluster (wrong) at
    0.61 -- a gap of 0.02. Every component here is deployed onto a
    cluster via a kubeconfig file, so they all share that vocabulary;
    similarity alone measures topic overlap across the whole sentence,
    not which component the sentence is actually instructing the system
    to build, and no fixed threshold can separate scores that close. The
    live path now folds this function's own signal into
    _llm_select_skills()'s prompt as a hint instead -- RAG surfaces the
    evidence, a real judgement call still decides. Left in place, not
    removed, same reason every other retired-but-not-deleted path in this
    module is kept."""
    ranked = grounding.relevant_skills(text)
    if not ranked:
        return None
    return sorted({skill for skill, score in ranked if score >= SKILL_MATCH_THRESHOLD})


# How close a top margin has to sit to SKILL_MATCH_THRESHOLD, on either
# side, to count as genuinely borderline rather than a clear accept or
# reject -- see selection_ambiguity(). Same status as SKILL_MATCH_THRESHOLD
# itself: a starting point, not yet measured against real requests.
SELECTION_AMBIGUITY_BAND = float(os.environ.get("SELECTION_AMBIGUITY_BAND", "0.1"))


def selection_ambiguity(text: str) -> dict | None:
    """None when the top skill match is clearly above or clearly below
    SKILL_MATCH_THRESHOLD; otherwise a dict describing exactly how
    borderline it was, for graph.py's confirm_selection node to pause on
    and ask the user about, instead of _rag_select_skills() silently
    deciding either way.

    This is a general mechanism, not a fix aimed at one known sentence:
    a check that fires whenever the system's own
    confidence is genuinely low, on any skill, for any reason -- the
    margin already measures exactly that, it was just being used as a
    yes/no cutoff before and never surfaced as a question.

    None whenever there's no real score to judge ambiguity by at all
    (RAG off, or the call failed) -- no signal, so nothing to ask about;
    the caller falls through to _word_match_skills() same as always."""
    ranked = grounding.relevant_skills(text)
    if not ranked:
        return None
    ranked.sort(key=lambda pair: -pair[1])
    top_skill, top_margin = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    # Logged for every message: the threshold and band are placeholders
    # until they are set from scores like these.
    logger.info(
        "selection: top score %.3f (%s), next %s; pauses within %.2f of %.2f",
        top_margin, top_skill,
        f"{runner_up[1]:.3f} ({runner_up[0]})" if runner_up else "none",
        SELECTION_AMBIGUITY_BAND, SKILL_MATCH_THRESHOLD,
    )
    if abs(top_margin - SKILL_MATCH_THRESHOLD) > SELECTION_AMBIGUITY_BAND:
        return None
    return {
        "top_skill": top_skill,
        "margin": top_margin,
        "runner_up": runner_up[0] if runner_up else None,
        "runner_up_margin": runner_up[1] if runner_up else None,
    }


def select_skills(text: str, *, ask: Optional[Callable] = None) -> List[str]:
    """Skill names relevant to `text`, sorted for a stable prompt.

    The real judgement call decides (see _llm_select_skills) -- RAG's
    embedding match is folded into that call's own prompt as a hint, not
    used on its own to decide anything (see _rag_select_skills's
    docstring for the measured reason a pure score can't make this call:
    "valkey cache" and "rke2-cluster" scored 0.63 and 0.61 on the exact
    same real request, because nearly every component shares the same
    cluster/kubeconfig vocabulary). Literal trigger-word matching is the
    final fallback, only when no model call is available this turn. A
    request is never left with no selection at all just because a call
    failed."""
    picked = _llm_select_skills(text, ask)
    return picked if picked is not None else _word_match_skills(text)


# A TPM limiter reads gateway.events.enriched, so it needs the enricher
# and, through it, the tokenizer. That edge depends on the policy's type,
# which DEPENDS_ON can't hold; the request's wording can.
_TPM = re.compile(r"\btpm\b|tokens?[ -]per[ -]min|token (?:limit|rate)", re.IGNORECASE)


def requested_skills(text: str, *, ask: Optional[Callable] = None) -> List[str]:
    """select_skills, plus the enricher for a tokens-per-minute limit."""
    named = select_skills(text, ask=ask)
    if "policy" in named and "enricher" not in named and _TPM.search(text):
        return sorted([*named, "enricher"])
    return named


_KUBECONFIG = re.compile(r"\bkubeconfig\b|\.kube/|rke2\.ya?ml\b", re.IGNORECASE)
# "new" and "cluster" in one phrase, with at most a few describing words
# between them ("a new three-node RKE2 cluster"). A word that starts another
# phrase ends it: "a new cache on the cluster" asks for a cache, and read as
# a new cluster it had the agent asking for node addresses.
_NEW_CLUSTER = re.compile(
    r"\bnew,?\s+"
    r"(?:(?!(?:to|on|onto|in|into|for|at|with|from|of|the|our|my|your|this|that|and|or)\b)"
    r"[\w+/-]+(?:\.[\w+/-]+)*\s+){0,3}"
    r"cluster\b",
    re.IGNORECASE,
)


def wants_new_cluster(text: str) -> bool:
    """Whether `text` asks for a new cluster to be built. A database's or a
    cache's own cluster ("a new Postgres cluster") is that component, not
    a cluster to build."""
    return bool(_NEW_CLUSTER.search(_NOT_AN_RKE2_CLUSTER.sub(r"\1", text.lower())))


def onto_existing_cluster(text: str) -> bool:
    """Whether the request is for a cluster that already exists: it says
    so, or it gives the cluster's kubeconfig, and doesn't ask for a new one."""
    if wants_new_cluster(text):
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in _EXISTING_HINTS) or bool(_KUBECONFIG.search(text))


def cluster_choice_given(text: str) -> bool:
    """Whether `text` says, either way, whether this builds onto a new
    cluster or an existing one -- used by graph.py's execute_tools() to
    tell a genuine new-vs-existing ambiguity apart from the model simply
    having forgotten to pass a kubeconfig_path it already has. Exposed
    separately from onto_existing_cluster() because that one only
    answers "existing" vs "not clearly existing" -- "new cluster" is
    itself a real, different answer, not the absence of one."""
    return wants_new_cluster(text) or onto_existing_cluster(text)


def _rag_dependencies_for(skills: Sequence[str]) -> Dict[str, List[str]]:
    """Each of `skills`' DIRECT (one-hop) dependencies, as skill names --
    orchestration/grounding.py's dependencies_for(), read by exact tag,
    no embedding call (same reasoning as field_docs(): the scope is
    already known, there is nothing to search for). Translates to/from
    DEPENDS_ON's own parameter-name space around the call, the same
    translation the old _needs() did around a direct dict lookup.

    Falls back to DEPENDS_ON itself for any skill RAG didn't answer for.
    This is the one place completeness is actively protected: a
    multi-hop dependency chain (chain() below walks this breadth-first)
    has to come back complete every single time, or a generated script
    silently ends up missing a real requirement -- the opposite of what
    moving this onto RAG is meant to achieve. See the rollout plan's
    note on why dependency lookup stays an exact tag filter, never a
    similarity search, for exactly this reason."""
    params = {SKILL_TO_PARAM.get(s, s): s for s in skills}
    found = grounding.dependencies_for(list(params))
    result: Dict[str, List[str]] = {}
    for param, skill in params.items():
        deps = found[param] if param in found else list(DEPENDS_ON.get(param, ()))
        result[skill] = [PARAM_TO_SKILL.get(d, d) for d in deps]
    return result


def chain(text: str, *, ask: Optional[Callable] = None) -> List[str]:
    """The components a request builds. Not, on a cluster that already
    exists, the cluster itself, nor block storage unless asked for -- both
    are for a new one; nor a model server when the user names the one they
    run (see full_stack.against_the_user).

    `ask`, when given, is the chat model's own judgement call for which
    components `text` really asks for (see select_skills) -- without it
    this falls back to trigger-word matching instead. _reference()'s own
    call to this function (orchestration/grounding.py's upfront field
    lookup) deliberately leaves it unset: a false positive there only
    costs an extra, unneeded field lookup, not a forced whole-stack
    compose, and doesn't need a model call every turn -- select() below,
    the one that actually decides what gets built, always passes a real
    one.

    Ordered so that a lookup cut by a budget keeps what matters most: the
    named components in the order the request names them -- a request
    leads with what it wants, "build a tokenizer" before the Postgres it
    also gives the name of, "add a gateway" before the rate limiter it
    only mentions -- then what they need, nearest first."""
    named = requested_skills(text, ask=ask)
    members = set(named)
    direct: Dict[str, List[str]] = {}
    frontier = list(named)
    while frontier:
        hop = _rag_dependencies_for(frontier)
        direct.update(hop)
        next_frontier = []
        for s in frontier:
            for dep in hop.get(s, ()):
                if dep not in members:
                    members.add(dep)
                    next_frontier.append(dep)
        frontier = next_frontier
    if onto_existing_cluster(text):
        members.discard("rke2-cluster")
        if "storage" not in named:
            members.discard("storage")
    if model_server_given(text):
        members.discard("inference")
    lowered = _normalise(text)
    terms = triggers()

    def mentioned_at(skill: str) -> int:
        found = [i for t in terms.get(skill, ()) if (i := _first(lowered, t)) is not None]
        return min(found, default=len(lowered))

    order = sorted((s for s in named if s in members), key=lambda s: (mentioned_at(s), s))
    frontier = list(order)
    while frontier:
        frontier = [d for s in frontier for d in direct.get(s, ()) if d in members and d not in order]
        order += list(dict.fromkeys(frontier))
    return order + sorted(members - set(order))


def _has_dependencies(skill: str) -> bool:
    return bool(DEPENDS_ON.get(SKILL_TO_PARAM.get(skill, skill)))


def _rag_tool_for(skills: Sequence[str]) -> Dict[str, str]:
    """Each of `skills`' single-resource tool name, read from RAG
    (orchestration/grounding.py's tool_for(), exact tag, no embedding
    call), falling back to TOOL_FOR_SKILL for anything RAG didn't answer
    for. A skill with no entry in either has no single-resource tool of
    its own -- only ever built through the whole-stack tool -- same as
    it simply not appearing in TOOL_FOR_SKILL meant before this moved."""
    found = grounding.tool_for(skills)
    result: Dict[str, str] = {}
    for s in skills:
        tool = found.get(s) or TOOL_FOR_SKILL.get(s)
        if tool:
            result[s] = tool
    return result


def wants_whole_stack(text: str, skills: Sequence[str]) -> bool:
    """Whether build_full_stack_plan should be offered for this request.

    True when the request says so outright, when it names a cluster plus
    something that installs onto one, or when it names any component that
    depends on others (mcp_tools/architecture.py) -- a tokenizer is only
    useful behind an enricher, gateway and event backbone, and only the
    whole-stack tool builds that chain -- onto an existing cluster too,
    through its `kubeconfig_path`.

    Otherwise False when the request says the cluster already exists: for
    a component with no dependencies the single-resource tool is the whole
    job, and offering the whole-stack tool there is how "add storage to my
    existing cluster" once became a script that provisions a second one.
    """
    lowered = text.lower()
    if any(hint in lowered for hint in _WHOLE_STACK_HINTS):
        return True
    if any(_has_dependencies(s) for s in skills):
        return True
    if any(hint in lowered for hint in _EXISTING_HINTS):
        return False
    return "rke2-cluster" in skills and len(skills) > 1


# Rough, dependency-free size estimate. tiktoken would be exact but is not
# a declared dependency of this layer (see requirements.txt), and four
# characters per token is close enough for a budget that already keeps a
# wide margin.
_CHARS_PER_TOKEN = 4

# What the prompt may cost before the conversation itself is counted. The
# default suits a 32,768-token window, leaving roughly 8k for the
# exchange; LLM_PROMPT_BUDGET_TOKENS raises it for a larger model (gpt-oss
# has 131k), where a component's whole dependency chain fits untrimmed.
# Selection is trimmed to fit rather than allowed to exceed it, because an
# over-long prompt does not degrade -- it fails outright, and every
# request fails with it.
PROMPT_BUDGET_TOKENS = 24_000


def _budget() -> int:
    return int(os.environ.get("LLM_PROMPT_BUDGET_TOKENS") or PROMPT_BUDGET_TOKENS)


def _estimate(skills: Sequence[str], tools: Sequence[str]) -> int:
    """Approximate prompt tokens for a given selection."""
    import json

    from orchestration.knowledge import BASE_INSTRUCTIONS
    from orchestration.tools import ALL_TOOLS

    chars = len(BASE_INSTRUCTIONS)
    chars += sum(len(c) for n, c in load_skills() if n in set(skills))
    wanted = set(tools)
    for tool in ALL_TOOLS:
        if tool.name in wanted:
            chars += len(json.dumps(tool.args_schema.model_json_schema()))
            chars += len(tool.description or "")
    return chars // _CHARS_PER_TOKEN


def _scores(text: str) -> Dict[str, int]:
    """How many distinct declared terms each skill matched -- used only to
    decide what to give up first when a selection has to be trimmed."""
    lowered = _normalise(text)
    return {
        name: sum(1 for term in terms if _matches(lowered, term))
        for name, terms in triggers().items()
    }


def select(
    text: str,
    already: Iterable[str] = (),
    budget: Optional[int] = None,
    *,
    ask: Optional[Callable] = None,
) -> Tuple[List[str], List[str]]:
    """The (skills, tools) a request needs, folded in with `already`.

    `ask`, passed through to chain(), is the chat model's own judgement
    call for which components `text` really asks for (see
    select_skills()'s own docstring for why this decides, not RAG's
    embedding match alone). Callers pass graph.py's _ask_unbound; without
    it this falls back to trigger-word matching instead.

    `already` is what earlier turns in the same thread pulled in. A
    follow-up like "now attach that policy to the gateway" names both, but
    "set its limit to 60" names neither -- carrying the thread's existing
    selection forward is what keeps the second kind working.

    The result is trimmed to `budget`, giving things up in this order:

    1. Skills carried from earlier turns that this message did not name.
       By the fourth turn of a long conversation everything mentioned so
       far had accumulated, and that -- not any single request -- is what
       exhausted the budget.
    2. The whole-stack tool, unless this message actually asked for a
       whole platform. Its schema is ~11k, the largest single item.
    3. The weakest-matching remaining skills, and their tools with them.

    The single-resource tools are never given up to keep the whole-stack
    one. That ordering was tried and is wrong: build_full_stack_plan
    always renders `RKE2Backend().create(cluster)`, so it is not a
    superset of them -- left as the only tool for "add a gateway to my
    existing cluster", it produces a script that provisions a second
    cluster. Trimming loses the model some rules; overflowing loses it
    everything; but neither is worth a plausible script that builds the
    wrong thing.
    """
    budget = budget or _budget()
    # A component brings its whole dependency chain: "build a tokenizer"
    # needs the rules for the enricher, gateway, control plane and queue
    # too, or the model has nothing to build them from.
    # On an existing cluster the cluster-building and Longhorn rules had the
    # model asking for node addresses after the user gave a kubeconfig, so
    # chain() leaves them out.
    current = set(chain(text, ask=ask))
    carried = [s for s in already if s not in current]
    skills = sorted(current) + [s for s in carried]
    whole_stack = wants_whole_stack(text, skills)
    # Fetched once for every candidate skill, not per _tools_for() call
    # below (budget trimming calls it repeatedly) -- one lookup, cached,
    # not one RAG round trip per trimming iteration.
    tool_for = _rag_tool_for(skills)

    def _tools_for(names: Sequence[str]) -> List[str]:
        # A component with dependencies is only offered through the
        # whole-stack tool. Its single-resource tool builds it alone, which
        # is how "build a tokenizer" produced a tokenizer and nothing else.
        chosen = {
            tool_for[s] for s in names
            if s in tool_for and not (whole_stack and _has_dependencies(s))
        }
        if whole_stack:
            chosen.add(WHOLE_STACK_TOOL)
        return sorted(chosen)

    if _estimate(skills, _tools_for(skills)) <= budget:
        return sorted(skills), _tools_for(skills)

    # 1. Shed the carried-over context first.
    while carried and _estimate(skills, _tools_for(skills)) > budget:
        dropped = carried.pop()
        skills = [s for s in skills if s != dropped]
    if _estimate(skills, _tools_for(skills)) <= budget:
        return sorted(skills), _tools_for(skills)

    # 2. Then the whole-stack tool, if this message didn't ask for one and
    #    names nothing that needs its dependency chain built. When it does,
    #    that tool is the only correct one -- dropping it would put the
    #    single-resource tools back, and a lone component with them.
    needs_chain = any(_has_dependencies(s) for s in requested_skills(text))
    if whole_stack and not needs_chain and not any(h in text.lower() for h in _WHOLE_STACK_HINTS):
        whole_stack = False
        if _estimate(skills, _tools_for(skills)) <= budget:
            return sorted(skills), _tools_for(skills)

    # 3. Then the weakest matches, tools included -- never the reverse.
    #    Among skills the message didn't name, a direct dependency of one it
    #    did (a tokenizer's enricher) outlasts one further down the chain.
    scores = _scores(text)
    named = requested_skills(text)
    direct = {
        PARAM_TO_SKILL.get(dep, dep)
        for s in named for dep in DEPENDS_ON.get(SKILL_TO_PARAM.get(s, s), ())
    }
    ordered = sorted(skills, key=lambda s: (scores.get(s, 0), s in direct, s))
    while len(ordered) > 1 and _estimate(skills, _tools_for(skills)) > budget:
        dropped = ordered.pop(0)
        skills = [s for s in skills if s != dropped]
    return sorted(skills), _tools_for(skills)
