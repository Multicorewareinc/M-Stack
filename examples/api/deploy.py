"""Deploy the API and UI layers onto a cluster this SDK built.

    python examples/api/deploy.py

Four capabilities, each a validated spec rather than a bag of chart
values: the policy services that answer `POST /check`, the gateway that
calls them, the two control planes that serve the platform's own API, and
the two portals that put a browser in front of those. The order is not
arbitrary — the gateway needs each policy's in-cluster address, and a
portal needs its control plane's, and both addresses are properties of a
spec, so they exist as soon as the spec does and not before.

Two policies here, rpm and tpm, because one is not enough to show the
property that matters: the chain is a list, and adding to it is
configuration. The gateway image is identical either way (ADR-006).

Everything past proxying attaches by configuration (ADR-006), so the
values below deploy a working stack even when the counter store and the
event backbone are addresses this script points at rather than things it
deploys itself — see examples/cache/create.py and examples/nats/deploy.py
for those, or examples/full_stack.py's own `nats` stage for both staged
together. Leave the backbone empty and the limiter says so instead of
silently allowing everything.
"""
import os

from multistack import (
    ControlPlane,
    ControlPlaneBackend,
    Gateway,
    GatewayBackend,
    Policy,
    PolicyBackend,
    Portal,
    PortalBackend,
    Stack,
)

KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)

# Where the SDK's own layers ended up. The gateway proxies to the vLLM
# this SDK deployed; the limiter counts into the key/value store beside
# it and reads the gateway's events off the backbone.
UPSTREAM = os.environ.get("MULTISTACK_UPSTREAM",
                          "http://vllm-qwen.inference.svc:8000")
CACHE_URL = os.environ.get("MULTISTACK_CACHE_URL",
                           "redis://valkey.platform.svc:6379/0")
# What's actually running on this cluster today: the Queue capability's
# own Helm-managed NATS in `platform` (examples/queue/install.py), not
# its chart's own `nats`/`nats` default -- deployed there specifically to
# replace the hand-deployed NATS that used to run at that same address.
BACKBONE = os.environ.get("MULTISTACK_BACKBONE",
                          "nats://nats.platform.svc:4222")

# The Secret holding API_KEY. A name, never a value: credentials do not
# live in a spec, and `kubectl create secret --from-literal` would put
# the key in the process arguments. multistack.kube.apply pipes a
# manifest to stdin instead.
KEY_SECRET = os.environ.get("MULTISTACK_GATEWAY_SECRET", "gateway-keys")

MODEL = os.environ.get("MULTISTACK_MODEL", "Qwen2.5-0.5B-Instruct")

# -- the UI layer's own dependencies ------------------------------------
# The control planes need a database, and this example does not deploy
# one. Published into the stack so ControlPlane's REQUIRES is satisfied
# the way it would be by a real CNPG cluster — and credential-free, which
# is why it cannot serve as the chart's own DATABASE_URL. That one lives
# in the Secrets below.
DATABASE_URL = os.environ.get(
    "MULTISTACK_DATABASE_URL",
    "postgresql://platform@platform-db-rw.postgres.svc.cluster.local:5432/platform",
)

# Names, never values. Each must already hold DATABASE_URL, VALKEY_URL,
# JWT_SECRET and its API keys — and the two JWT_SECRETs must match, since
# each plane verifies tokens the other issued. The backend checks the
# keys before installing and names any that are missing.
ADMIN_CP_SECRET = os.environ.get("MULTISTACK_ADMIN_CP_SECRET", "admin-cp-secrets")
ORG_CP_SECRET = os.environ.get("MULTISTACK_ORG_CP_SECRET", "org-cp-secrets")

# Which node holds the portal images. There is no in-cluster registry, so
# they are built locally and sideloaded into one node's containerd — a
# pod scheduled anywhere else stays ImagePullBackOff. Empty leaves the
# chart's own default in place.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", "")


def main() -> None:
    # One statement of the shared facts, threaded into both specs.
    stack = Stack(
        kubeconfig_path=KUBECONFIG,
        cache_url=CACHE_URL,
        event_backbone_url=BACKBONE,
        api_key_secret=KEY_SECRET,
        # Satisfies ControlPlane's REQUIRES without deploying Postgres
        # here. A real run records a CNPG cluster instead, which publishes
        # the same key.
        database_url=DATABASE_URL,
    )

    # kubeconfig_path, cache_url and event_backbone_url all come from the
    # stack for both of these. Passing any of them here would win instead.
    #
    # The limits differ by more than their numbers: rpm counts requests
    # and tpm counts tokens, so 3 and 120000 are the same kind of
    # statement about the same model. Copying one to the other is the
    # mistake the tpm driver warns about, because 3 tokens a minute
    # denies everything and 120000 requests a minute denies nothing.
    limiters = [
        stack.build(Policy, type="rpm",
                    limits={"model_overrides": {MODEL: 3}}),
        stack.build(Policy, type="tpm",
                    limits={"model_overrides": {MODEL: 120_000}}),
    ]

    # Collected here rather than read back from the stack: `policy_endpoint`
    # is one key, so recording the second policy overwrites the first.
    # A list-valued output would be the fix, and is not the point of this
    # example.
    endpoints = []
    for limiter in limiters:
        for warning in PolicyBackend().check_prerequisites(limiter):
            print(f"[{limiter.type}] warning: {warning}")
        endpoints.append(PolicyBackend().create(limiter))
        stack.record(limiter)
        print(f"[{limiter.type}] answering /check at {endpoints[-1]}")

    # Both addresses, one list, no gateway change. The chain is ordered:
    # the gateway calls them in sequence and the first denial wins, so
    # rpm — the cheaper check — goes first.
    gateway = stack.build(
        Gateway,
        type="modelgateway",
        upstream_url=UPSTREAM,
        model_routes={MODEL: UPSTREAM},
        policy_endpoints=endpoints,
    )
    for warning in GatewayBackend().check_prerequisites(gateway):
        print(f"[gateway] warning: {warning}")

    print(f"[gateway] serving at {GatewayBackend().create(gateway)}")
    stack.record(gateway)

    # -- the UI layer ---------------------------------------------------
    # Each portal is paired with its own control plane in the same
    # iteration rather than read back from the stack. `controlplane_endpoint`
    # is one key, so recording the second plane overwrites the first — the
    # same shape as `policy_endpoint` above, and the reason Portal's
    # api_upstream is passed explicitly here even though FROM_STACK could
    # fill it.
    #
    # A portal proxies to a control plane, not to the gateway: the gateway
    # fronts inference, the control planes serve the portals' own API.
    node_selector = ({"kubernetes.io/hostname": IMAGE_NODE} if IMAGE_NODE
                     else None)

    for plane_type in ("admin", "organization"):
        secret = ADMIN_CP_SECRET if plane_type == "admin" else ORG_CP_SECRET
        plane = stack.build(
            ControlPlane, type=plane_type, existing_secret=secret,
            # Seeding runs on a first install and is not concurrency-safe:
            # two admin pods racing both insert the same plan and one dies
            # on the unique index. Scale up once it has run.
            replicas=1 if plane_type == "admin" else 2,
        )
        for warning in ControlPlaneBackend().check_prerequisites(plane):
            print(f"[{plane_type}-cp] warning: {warning}")
        print(f"[{plane_type}-cp] serving at {ControlPlaneBackend().create(plane)}")
        stack.record(plane)

        portal = stack.build(
            Portal, type=plane_type,
            api_upstream=plane.endpoint,
            node_selector=node_selector,
        )
        for warning in PortalBackend().check_prerequisites(portal):
            print(f"[{plane_type}-portal] warning: {warning}")
        print(f"[{plane_type}-portal] serving at {PortalBackend().create(portal)}")
        stack.record(portal)

    print("\nPublished by this run:")
    for key, value in sorted(stack.outputs.items()):
        print(f"  {key} = {value}")


if __name__ == "__main__":
    main()
