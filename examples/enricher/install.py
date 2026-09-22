"""
Install the enricher on an existing RKE2 cluster.

Sits between the gateway's raw event stream and everything downstream
that needs a token count off it (rate-limiter-tpm, billing). Consumes
`gateway.events`, falls back to the tokenizer when a response carries no
`usage` block, and republishes to a derived enriched stream that TPM and
billing both consume instead of calling the tokenizer themselves.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/enricher/install.py

Needs `helm` and `kubectl` on PATH, a reachable cluster, and an event
backbone (NATS) — without one this deploys but enriches nothing, and
this script refuses rather than let that pass silently.
"""
import os

from multistack import Enricher, EnricherBackend, EnricherOptions

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# Where the gateway's events flow. Empty is allowed, but only as a stated
# decision: the spec refuses to build an enricher with no backbone unless
# told to, because the result is deployed, healthy and enriching nothing.
#
# The default matches what's actually running on this cluster today: the
# Queue capability's own Helm-managed NATS in `platform`
# (examples/queue/install.py), not its chart's own `nats`/`nats` default
# -- deployed there specifically to replace the hand-deployed NATS that
# used to run at that same address.
EVENT_BACKBONE = os.environ.get(
    "MULTISTACK_EVENT_BACKBONE", "nats://nats.platform.svc.cluster.local:4222"
)
BACKBONE_ARGS = ({"event_backbone_url": EVENT_BACKBONE} if EVENT_BACKBONE
                 else {"allow_no_backbone": True})
if not EVENT_BACKBONE:
    print("[enricher] no event backbone: nothing will be enriched until "
          "one exists (MULTISTACK_EVENT_BACKBONE)")

# Optional fallback for a response event with no `usage` block. Empty
# leaves the fallback inert — those events are republished with
# source="none" rather than estimated, so they go uncounted downstream.
TOKENIZER_URL = os.environ.get("MULTISTACK_TOKENIZER_URL", "")

# There is no in-cluster registry, so the service image exists only on
# the node it was imported to; a pod scheduled anywhere else stays
# ImagePullBackOff. Empty leaves the chart's own default in place.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", "")
NODE_SELECTOR = {"kubernetes.io/hostname": IMAGE_NODE} if IMAGE_NODE else None

enricher = Enricher(
    kubeconfig_path=KUBECONFIG,
    **BACKBONE_ARGS,
    options=EnricherOptions(tokenizer_url=TOKENIZER_URL),
    node_selector=NODE_SELECTOR,
)

backend = EnricherBackend()
for warning in backend.check_prerequisites(enricher):
    print(f"[enricher] warning: {warning}")

endpoint = backend.create(enricher)

print("\nEnricher installed")
print(f"  endpoint:  {endpoint}  (health checks and metrics only)")
print("\nrate-limiter-tpm and billing both need to point at the derived "
      "stream this produces:")
print(f"  {enricher.options.enriched_stream_name} / "
      f"{enricher.options.enriched_subject}")
