"""
Install the authenticated gateway in front of an inference endpoint.

The front door: per-org API-key authentication (verified against the
organization control plane -- ADR-026 removed the older static-key mode),
an optional chain of rate-limit policies, and an optional event backbone
for usage accounting. Everything past authentication attaches by
configuration rather than code — an empty `policy_endpoints` builds no
policy client, an empty `event_backbone_url` builds no publisher — so
this file deploys a working authenticated proxy and each feature is one
field away.

    pip install -e ".[helm]"                  # from the repo root
    python3 examples/gateway/install.py

Needs `helm` and `kubectl` on PATH, a reachable cluster, an
OpenAI-compatible upstream to proxy to, and a running organization
control plane to verify keys against (see examples/controlplane/install.py
— deploy that first).

No credential is in this file and none reaches a command line. Both the
gateway's own API key and the bearer it uses against the organization
control plane are written to a Kubernetes Secret through
`multistack.kube.apply`, which pipes the manifest to stdin;
`kubectl create secret --from-literal` would put them in the process
arguments, where any local user can read them out of /proc.
"""
import base64
import os

from multistack import ControlPlane, Gateway, GatewayBackend, prompt_secret
from multistack.kube import apply

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# What to proxy. Published by the inference capability as
# `inference_endpoint` — see examples/inference/serve.py.
UPSTREAM = os.environ.get(
    "MULTISTACK_INFERENCE_ENDPOINT", "http://vllm-qwen.inference.svc.cluster.local:8000"
)

# Defaults to the spec's own namespace rather than repeating a value
# here: a second default in the example is one that silently goes
# stale the day the SDK's changes.
NAMESPACE = (os.environ.get("MULTISTACK_GATEWAY_NS")
             or Gateway.DEFAULT_NAMESPACES["modelgateway"])

# The policy chain, in order — the gateway calls them in sequence and the
# first denial wins, so the cheaper check goes first. Comma-separated, and
# empty by default: with no chain every authenticated request is forwarded
# with nothing checked, which check_prerequisites warns about rather than
# passing over in silence. The endpoints come from
# examples/policy/install.py.
POLICY_ENDPOINTS = [e.strip() for e in
                    os.environ.get("MULTISTACK_POLICY_ENDPOINTS", "").split(",")
                    if e.strip()]
KEY_SECRET = "gateway-keys"

# Asked for, not written down. The chart refuses to install without a key
# source, because the image ships a development default — a gateway that
# accepts a well-known key is not an authenticated one.
#
# generate=True: unlike a database password, nobody needs to *choose* an
# API key, so pressing Enter is the expected answer. It is printed at the
# end, which is the only moment it is ever shown.
API_KEY = prompt_secret(
    "Gateway API key",
    env_var="MULTISTACK_GATEWAY_API_KEY",
    generate=True,
)

# The bearer verify_api_key sends to the organization control plane's
# GET /internal/v1/api-keys/verify. Must be the IDENTICAL value as that
# release's secret.mgServiceApiKey — this script cannot set that one too
# (it is a different chart's Secret, created separately), so it can only
# say so loudly, the same way the two control planes' SERVICE_API_KEY
# already has to match by convention rather than by code.
ORG_VERIFY_API_KEY = prompt_secret(
    "Gateway's bearer against the organization control plane "
    "(must equal that release's secret.mgServiceApiKey exactly)",
    env_var="MULTISTACK_ORG_VERIFY_API_KEY",
    generate=True,
)

# Derived from the spec rather than typed out, so renaming the release or
# moving its namespace cannot leave the gateway calling nothing —
# existing_secret is irrelevant here, only .endpoint is read.
ORG_CP = ControlPlane(
    type="organization",
    kubeconfig_path=KUBECONFIG,
    existing_secret="unused-for-endpoint-only",
)
ORG_CP_URL = os.environ.get("MULTISTACK_ORG_CP_URL", ORG_CP.endpoint)

apply(KUBECONFIG, {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": KEY_SECRET, "namespace": NAMESPACE},
    "type": "Opaque",
    "data": {
        "API_KEY": base64.b64encode(API_KEY.encode()).decode(),
        "ORG_VERIFY_API_KEY":
            base64.b64encode(ORG_VERIFY_API_KEY.encode()).decode(),
    },
})
print(f"[gateway] wrote Secret {KEY_SECRET} in {NAMESPACE}")
print(f"[gateway] verifying keys against {ORG_CP_URL}")

# There is no in-cluster registry, so the service image exists only on
# the node it was imported to; a pod scheduled anywhere else stays
# ImagePullBackOff. Empty leaves the chart's own default in place.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", "")
NODE_SELECTOR = {"kubernetes.io/hostname": IMAGE_NODE} if IMAGE_NODE else None

gateway = Gateway(
    kubeconfig_path=KUBECONFIG,
    namespace=NAMESPACE,
    upstream_url=UPSTREAM,
    # By name. The key itself never enters the spec, so this file is safe
    # to commit.
    api_key_secret=KEY_SECRET,
    # Required, not optional configuration: ADR-026 removed the gateway's
    # static-key auth mode, so with no organization control plane to call,
    # every /v1 request fails closed with a 503 before it ever reaches
    # upstream_url.
    org_cp_internal_url=ORG_CP_URL,
    policy_endpoints=POLICY_ENDPOINTS,
    # closed: if the policy chain cannot be reached, reject. `open` would
    # forward instead — a deliberate choice to prefer availability over
    # enforcement, and the wrong default for anything that bills.
    policy_fail_mode="closed",
    node_selector=NODE_SELECTOR,
)

backend = GatewayBackend()
for warning in backend.check_prerequisites(gateway):
    print(f"[gateway] warning: {warning}")

endpoint = backend.create(gateway)

print("\nGateway installed")
print(f"  endpoint:  {endpoint}")
print(f"  upstream:  {gateway.upstream_url}")
print("\nTry it (the key is only shown here — it is not stored outside the Secret):")
print(f"""  curl {endpoint}/v1/chat/completions \\
    -H 'Authorization: Bearer {API_KEY}' \\
    -H 'Content-Type: application/json' \\
    -d '{{"model":"qwen","messages":[{{"role":"user","content":"hi"}}]}}'""")

print(f"\nORG_VERIFY_API_KEY (only shown here) must also be set as "
      f"{ORG_CP_URL.split('//')[1].split('.')[0]}'s "
      "secret.mgServiceApiKey, exactly:")
print(f"  {ORG_VERIFY_API_KEY}")
print("A mismatch here does not fail this install or any probe — it fails "
      "the first real request with a 503.")
