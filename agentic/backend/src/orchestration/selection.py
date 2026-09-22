"""Choosing which skills and tools a single request actually needs.

Loading all thirteen SKILL.md files and binding all fourteen tool schemas
on every request costs roughly 48,000 tokens before the user has typed
anything -- more than the 32,768-token context of the model this layer is
configured against, so every request overflowed. That cost also grew with
each capability added, so it was going to keep getting worse.

This is the selection step `docs/architecture-decision.md` calls Phase C,
using literal trigger matching rather than embeddings. Embeddings are the
better answer once the skill set is large enough to need ranking; matching
is enough while "does this request mention a database" is the actual
question, and it has no index to build, no model to call, and no way to
fail at run time.

Triggers are not maintained here. Every SKILL.md already declares its own
in frontmatter -- "Use this skill when the user's request mentions
Postgres, PostgreSQL, CloudNativePG, CNPG, or a database to deploy" -- and
that sentence is parsed for them. A new capability therefore needs nothing
added to this module, which is the same property that keeps the rest of
this layer resource-agnostic: a second place listing triggers by hand
would be a second place to drift.

Selection is deliberately generous. Including a skill the request did not
need costs tokens; missing one costs the model the rules it needs to avoid
guessing a value, which is the failure this whole layer exists to prevent.
So a partial match includes the skill, and anything resembling a
whole-platform request pulls in the composed tool as well.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from mcp_tools.architecture import DEPENDS_ON, PARAM_TO_SKILL, SKILL_TO_PARAM, skill_closure
from orchestration.knowledge import load_skills

# skills/<dir> -> the tool that builds that resource.
TOOL_FOR_SKILL: Dict[str, str] = {
    "rke2-cluster": "build_rke2_cluster_plan",
    "storage": "build_storage_plan",
    "minio": "build_minio_plan",
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
        meaningful = [w for w in words if len(w) > 2 and w not in _STOPWORDS]
        # The whole phrase, when it is more than one useful word.
        if len(meaningful) > 1:
            found.add(" ".join(words))
        found.update(meaningful)
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
    gateways and not policy. Multi-word phrases are untouched, so minio
    still answers to "object storage", and terms no directory claims --
    "admin", "organization" -- keep selecting every skill that named them,
    which is right: those two really do come in pairs.
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
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


# "cluster" after a database, cache or queue word names that component's own
# cluster -- "postgres cluster org-db" -- not an RKE2 one. Matched as the
# rke2-cluster trigger it loaded the cluster-building rules mid-conversation,
# and the model asked "new cluster or existing?" after the user had said.
_NOT_AN_RKE2_CLUSTER = re.compile(
    r"\b(postgres(?:ql)?|cnpg|database|db|valkey|redis|cache|nats|jetstream)\s+cluster\b"
)


# "ingress gateway" is one component; read word by word its "gateway" also
# selected the model gateway, and with it that gateway's whole dependency
# chain, for a request about the front door alone.
_INGRESS_GATEWAY = re.compile(r"\bingress[\s-]+gateway\b")


def _normalise(text: str) -> str:
    text = _NOT_AN_RKE2_CLUSTER.sub(r"\1", text.lower())
    return _INGRESS_GATEWAY.sub("ingressgateway", text)


def select_skills(text: str) -> List[str]:
    """Skill names relevant to `text`, sorted for a stable prompt."""
    lowered = _normalise(text)
    return sorted(
        name for name, terms in triggers().items()
        if any(_matches(lowered, term) for term in terms)
    )


def _has_dependencies(skill: str) -> bool:
    return bool(DEPENDS_ON.get(SKILL_TO_PARAM.get(skill, skill)))


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
) -> Tuple[List[str], List[str]]:
    """The (skills, tools) a request needs, folded in with `already`.

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
    current = set(skill_closure(select_skills(text)))
    carried = [s for s in already if s not in current]
    skills = sorted(current) + [s for s in carried]
    whole_stack = wants_whole_stack(text, skills)

    def _tools_for(names: Sequence[str]) -> List[str]:
        # A component with dependencies is only offered through the
        # whole-stack tool. Its single-resource tool builds it alone, which
        # is how "build a tokenizer" produced a tokenizer and nothing else.
        chosen = {
            TOOL_FOR_SKILL[s] for s in names
            if s in TOOL_FOR_SKILL and not (whole_stack and _has_dependencies(s))
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
    needs_chain = any(_has_dependencies(s) for s in select_skills(text))
    if whole_stack and not needs_chain and not any(h in text.lower() for h in _WHOLE_STACK_HINTS):
        whole_stack = False
        if _estimate(skills, _tools_for(skills)) <= budget:
            return sorted(skills), _tools_for(skills)

    # 3. Then the weakest matches, tools included -- never the reverse.
    #    Among skills the message didn't name, a direct dependency of one it
    #    did (a tokenizer's enricher) outlasts one further down the chain.
    scores = _scores(text)
    named = select_skills(text)
    direct = {
        PARAM_TO_SKILL.get(dep, dep)
        for s in named for dep in DEPENDS_ON.get(SKILL_TO_PARAM.get(s, s), ())
    }
    ordered = sorted(skills, key=lambda s: (scores.get(s, 0), s in direct, s))
    while len(ordered) > 1 and _estimate(skills, _tools_for(skills)) > budget:
        dropped = ordered.pop(0)
        skills = [s for s in skills if s != dropped]
    return sorted(skills), _tools_for(skills)
