"""The platform's dependency order, from its architecture diagram.

One source for two jobs: selection expands a request for one component
into everything it needs (orchestration/selection.py), and
build_full_stack_plan rejects a plan that leaves any of it out. Keyed by
build_full_stack_plan's own parameter names.

The order, dependencies first:

  Baseline, needed by every request: Postgres (cnpg) and Redis (valkey)
  -> the organization control plane -> the Model Gateway, which verifies
  every API key against that control plane.

  Events: the gateway publishes to NATS gateway.events (queue). The
  enricher consumes that, falls back to the tokenizer for a token count,
  and republishes to gateway.events.enriched. The RPM limiter reads the
  raw stream; the TPM limiter and billing read the enriched one, so they
  need the enricher and tokenizer too.

  Management: each portal needs its control plane, and each control
  plane its Postgres and Redis.

Some edges also depend on a field (the TPM limiter needs the enricher, an
organization portal needs the organization control plane); those checks
stay in build_full_stack_plan, next to their specific error messages.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple

DEPENDS_ON: Dict[str, Tuple[str, ...]] = {
    "storage": (),
    "minio": ("storage",),
    "observability": ("storage",),
    "queue": ("storage",),
    "accelerator": (),
    "inference": (),
    "valkey": (),
    "cnpg": (),
    "controlplane": ("cnpg", "valkey"),
    "portal": ("controlplane",),
    "gateway": ("controlplane",),
    "policy": ("gateway", "valkey", "queue"),
    "enricher": ("gateway", "queue", "tokenizer"),
    "tokenizer": ("enricher",),
    "billing": ("cnpg", "enricher"),
    "ingress": (),
    "route": ("ingress",),
}

# Why each edge exists, for the error build_full_stack_plan returns when a
# plan leaves the dependency out.
REASON: Dict[Tuple[str, str], str] = {
    ("policy", "gateway"): "a rate limiter is called by the gateway on every request -- without one it limits nothing",
    ("policy", "valkey"): "the limiter keeps its RPM/TPM counters in Redis",
    ("policy", "queue"): "the limiter's counters advance from the gateway's events on NATS",
    ("enricher", "gateway"): "it consumes the gateway's raw events -- without a gateway nothing is ever published",
    ("enricher", "queue"): "it reads gateway.events and publishes gateway.events.enriched, both on NATS",
    ("enricher", "tokenizer"): "the tokenizer is its synchronous fallback when an event has no token count",
    ("tokenizer", "enricher"): "the enricher is the tokenizer's only caller -- on its own it counts nothing",
    ("queue", "storage"): "JetStream persists its streams to a StorageClass",
}

# selection.py works in skill names; a few differ from the parameter.
SKILL_TO_PARAM: Dict[str, str] = {"rke2-cluster": "cluster", "ingress-gateway": "ingress"}
PARAM_TO_SKILL: Dict[str, str] = {v: k for k, v in SKILL_TO_PARAM.items()}


def closure(names: Iterable[str]) -> List[str]:
    """`names` plus everything they depend on, transitively, sorted."""
    seen: set = set()
    stack = list(names)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(DEPENDS_ON.get(name, ()))
    return sorted(seen)


def skill_closure(skills: Iterable[str]) -> List[str]:
    """closure() over skill names instead of parameter names."""
    params = [SKILL_TO_PARAM.get(s, s) for s in skills]
    return sorted(PARAM_TO_SKILL.get(p, p) for p in closure(params))
