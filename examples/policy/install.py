"""
Install a rate-limiting policy on an existing RKE2 cluster.

Two implementations, one capability. `rpm` counts requests per minute;
`tpm` counts tokens per minute, off a *different* stream from rpm's raw
one — the `enricher` service's derived `gateway.events.enriched`
(ADR-030), which guarantees every event carries a usable token count, so
tpm itself has no tokenizer dependency at all. Pick one with `type=`, or
install both — they are separate releases and the gateway can call a
chain of them.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/policy/install.py

Needs `helm` and `kubectl` on PATH, a reachable cluster, and a cache
(Valkey) already running — the counters live there, so a policy with no
cache has nowhere to keep state.
"""
import os

from multistack import Policy, PolicyBackend
from multistack.policy.spec import RateLimits

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# Where the counters live. Published by the cache capability as
# `cache_url` — see examples/cache/create.py — and credential-free by
# design: the password, if any, comes from the release's own Secret.
CACHE_URL = os.environ.get(
    "MULTISTACK_CACHE_URL", "redis://valkey-primary.cache.svc.cluster.local:6379/0"
)

# Where the gateway publishes its request events. Counting is
# asynchronous: /check only *reads* the counters, and a JetStream consumer
# advances them from those events. With no backbone the counters never
# move and /check allows everything — a limiter that is deployed, healthy
# and enforcing nothing. The spec refuses to build without this rather
# than let that happen; set allow_no_backbone=True to do it deliberately.
#
# The default matches what's actually running on this cluster today: the
# Queue capability's own Helm-managed NATS in `platform`
# (examples/queue/install.py), not its chart's own `nats`/`nats` default
# -- deployed there specifically to replace the hand-deployed NATS that
# used to run at that same address.
EVENT_BACKBONE = os.environ.get(
    "MULTISTACK_EVENT_BACKBONE", "nats://nats.platform.svc.cluster.local:4222"
)

# Empty is allowed, but only as a stated decision: the spec refuses to
# build a limiter with no backbone unless told to, because the result is
# deployed, healthy and enforcing nothing. Set the variable to "" to
# bring the services up ahead of a backbone.
BACKBONE_ARGS = ({"event_backbone_url": EVENT_BACKBONE} if EVENT_BACKBONE
                 else {"allow_no_backbone": True})
if not EVENT_BACKBONE:
    print("[policy] no event backbone: /check will allow every request "
          "until one exists (MULTISTACK_EVENT_BACKBONE)")

# There is no in-cluster registry, so the service image exists only on
# the node it was imported to; a pod scheduled anywhere else stays
# ImagePullBackOff. Empty leaves the chart's own default in place.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", "")
NODE_SELECTOR = {"kubernetes.io/hostname": IMAGE_NODE} if IMAGE_NODE else None

backend = PolicyBackend()

# Requests per minute. The cheap check: it needs no token accounting and
# so no tokenizer, and it rejects before the request reaches the model.
rpm = Policy(
    type="rpm",
    kubeconfig_path=KUBECONFIG,
    cache_url=CACHE_URL,
    **BACKBONE_ARGS,
    # Requests per minute. Zero means unlimited for that scope, not
    # blocked — the inverse of what most people assume.
    limits=RateLimits(user_default=60, model_default=600),
    node_selector=NODE_SELECTOR,
)

# Tokens per minute. The one that actually maps to cost. Needs the
# `enricher` service running and consuming the gateway's raw events —
# without it the enriched stream tpm subscribes to never has anything
# published to it, and counters never advance.
tpm = Policy(
    type="tpm",
    kubeconfig_path=KUBECONFIG,
    cache_url=CACHE_URL,
    **BACKBONE_ARGS,
    # Tokens per minute. The same field names as rpm above, but the
    # unit comes from the type — a limits document copied between the two
    # is either trivially open or instantly closed, and both are valid
    # documents so nothing can reject it. The tpm driver warns when a
    # ceiling is small enough to look like a request count.
    limits=RateLimits(user_default=100_000, model_default=1_000_000),
    node_selector=NODE_SELECTOR,
)

for policy in (rpm, tpm):
    for warning in backend.check_prerequisites(policy):
        print(f"[{policy.type}] warning: {warning}")
    endpoint = backend.create(policy)
    print(f"\n{policy.type} policy installed")
    print(f"  endpoint:  {endpoint}")

print("\nAttach them to a gateway, in order — the chain is evaluated as listed:")
print(f"  Gateway(..., policy_endpoints=[{rpm.endpoint!r}, {tpm.endpoint!r}])")
