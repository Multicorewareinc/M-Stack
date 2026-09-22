"""
Lock down inbound access to services already deployed by this SDK.

NetworkPolicy is an opt-in field on each capability's spec -- empty by
default (no policy installed, an existing deployment is untouched), and
every service's `create()` is an upsert (`helm upgrade --install`, or
`kubectl apply`), so turning this on later is just re-running create()
with the field now set -- no need to tear anything down first. See
multistack/netpolicy/README.md for the full design: why it's ingress-only,
why the allow-list is never guessed, and how to find a real selector on
your own cluster instead of the illustrative ones below.

    pip install -e .                              # from the repo root
    python3 examples/network_policy/lockdown.py

Assumes you already have vLLM (examples/inference/serve.py), a MinIO
tenant (examples/minio/tenant.py), and your own model-gateway/rate-limiter
releases running -- this only adds NetworkPolicy on top of them. The
selectors below (e.g. `app.kubernetes.io/name: model-gateway`) are the
labels this SDK's own charts/drivers actually set; confirm yours with
`kubectl get pods --show-labels` before trusting them on a different
cluster, per the README's "Finding the real selector" section.
"""
import os

from multistack import Gateway, GatewayBackend, Inference, InferenceBackend, MinIOTenant, Policy, PolicyBackend
from multistack.backends.minio_client import MinIOBackend

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# model-gateway's chart labels its own pods this way -- see "Finding the
# real selector" in multistack/netpolicy/README.md. It's the only caller
# vLLM and both rate limiters ever need to hear from.
MODEL_GATEWAY_LABEL = {"app.kubernetes.io/name": "model-gateway"}

# -- vLLM: only the gateway may call in ------------------------------------
inference = Inference(
    kubeconfig_path=KUBECONFIG,
    name="vllm",
    allowed_client_labels=[MODEL_GATEWAY_LABEL],
)
InferenceBackend().create(inference)

# -- model-gateway: only your ingress edge may call in ----------------------
# Replace with your real ingress selector (e.g. the Istio ingress
# gateway's pods) -- there's no ingress-gateway chart/label wired into
# this SDK to confirm one against, so this is illustrative only.
gateway = Gateway(
    kubeconfig_path=KUBECONFIG,
    upstream_url=f"http://{inference.name}.{inference.resolved_namespace}.svc.cluster.local:{inference.port}",
    api_key_secret="gateway-keys",
    network_policy_allowed_ingress=[{"istio": "ingressgateway"}],
)
GatewayBackend().create(gateway)

# -- both rate limiters: only the gateway may call in ------------------------
# allow_no_backbone=True keeps this example focused on NetworkPolicy
# rather than requiring a real NATS -- a real deployment sets
# event_backbone_url instead (see multistack/policy/README equivalents,
# the chart's own values.yaml comments).
rpm = Policy(
    type="rpm", kubeconfig_path=KUBECONFIG, cache_url="redis://valkey-primary.cache.svc.cluster.local:6379/0",
    allow_no_backbone=True, network_policy_allowed_ingress=[MODEL_GATEWAY_LABEL],
)
PolicyBackend().create(rpm)

tpm = Policy(
    type="tpm", kubeconfig_path=KUBECONFIG, cache_url="redis://valkey-primary.cache.svc.cluster.local:6379/0",
    allow_no_backbone=True, network_policy_allowed_ingress=[MODEL_GATEWAY_LABEL],
)
PolicyBackend().create(tpm)

# -- MinIO: only whatever mirrors weights from it may call in ----------------
tenant = MinIOTenant(
    kubeconfig_path=KUBECONFIG,
    name="models",
    network_policy_allowed_ingress=[{"app": inference.name}],
)
MinIOBackend().create(tenant)

print("NetworkPolicy applied for: vllm, model-gateway, rate-limiter-rpm, "
      "rate-limiter-tpm, minio tenant 'models'.")
print("Verify with: kubectl get networkpolicy -A")
