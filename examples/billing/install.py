"""
Install billing on an existing RKE2 cluster.

Usage metering and Stripe subscriptions, off the enricher's derived event
stream (see examples/enricher/install.py — this must be running and
consuming for anything to be metered at all). Stripe reporting is
genuinely optional: leave STRIPE_SECRET_KEY/STRIPE_WEBHOOK_SECRET out of
the Secret below for a usage-metering-only deployment.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/billing/install.py

Needs `helm` and `kubectl` on PATH, a reachable cluster, a Postgres
database for this service alone, and the Secret named below.

No credential appears in this file. The chart reads DATABASE_URL and
SERVICE_API_KEY (and optionally the two Stripe secrets) from a Kubernetes
Secret, and the spec carries only that Secret's name — a spec is a file
people commit. Create the Secret with `multistack.kube.apply`, which
pipes the manifest to stdin; `kubectl create secret --from-literal` puts
the value in the process arguments, where any local user can read it.
"""
import os

from multistack import Billing, BillingBackend

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# Must carry every key in multistack.billing.REQUIRED_SECRET_KEYS
# (DATABASE_URL, SERVICE_API_KEY), plus optionally STRIPE_SECRET_KEY and
# STRIPE_WEBHOOK_SECRET. SERVICE_API_KEY must equal admin-control-plane's
# own SERVICE_API_KEY exactly — it is the outbound bearer admin-cp reuses
# to notify this service of org/plan changes.
BILLING_SECRET = os.environ.get("MULTISTACK_BILLING_SECRET", "billing-secrets")

# Where the enricher's derived stream flows. Empty is allowed, but only
# as a stated decision: the spec refuses to build a billing service with
# no backbone unless told to, because the result is deployed, healthy and
# metering nothing.
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
    print("[billing] no event backbone: nothing will be metered until "
          "one exists (MULTISTACK_EVENT_BACKBONE)")

# There is no in-cluster registry, so the service image exists only on
# the node it was imported to; a pod scheduled anywhere else stays
# ImagePullBackOff. Empty leaves the chart's own default in place.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", "")
NODE_SELECTOR = {"kubernetes.io/hostname": IMAGE_NODE} if IMAGE_NODE else None

billing = Billing(
    kubeconfig_path=KUBECONFIG,
    existing_secret=BILLING_SECRET,
    **BACKBONE_ARGS,
    node_selector=NODE_SELECTOR,
)

backend = BillingBackend()
for warning in backend.check_prerequisites(billing):
    print(f"[billing] warning: {warning}")

endpoint = backend.create(billing)

print("\nBilling installed")
print(f"  endpoint:  {endpoint}")
print("\nWire admin-control-plane's BILLING_INTERNAL_URL at it by hand --")
print("that chart has no values.yaml hook for it yet (ARCHITECTURE-GAPS.md):")
print(f"  BILLING_INTERNAL_URL={endpoint!r}")
