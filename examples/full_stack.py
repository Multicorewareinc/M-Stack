"""
Build the whole stack, bare machines to a served model, in one run.

This is the composition the per-component examples only hint at:

    RKE2       a cluster on bare machines
      -> Storage     block storage (Longhorn), which RKE2 ships without
      -> MinIO       object storage, on top of that block storage
      -> Inference   a model served from that object storage (vLLM)

Each layer's output is the next one's input. The kubeconfig threads
through all four; Longhorn provides the StorageClass MinIO's PVCs bind
to; MinIO's endpoint is where vLLM reads its weights — so the inference
pod never needs internet access at all.

    pip install -e .                          # from the repo root
    python3 examples/full_stack.py

DESTRUCTIVE. Stage 1 installs RKE2 on every machine in NODES, so point
it at hosts you are willing to have reformatted. Needs `ssh`, `helm` and
`kubectl` on PATH, passwordless key-based SSH to every node, and a user
that is root or has non-interactive `sudo -n`.

Re-running is safe: every stage is idempotent. Trim STAGES to re-run
just part of it — the later stages read what the earlier ones left
behind rather than depending on values passed in memory.

Not everything here is SDK surface. Bucket and object management, node
labels and Secrets are deliberately outside the components, so this
file does them with `kubectl` — from
`multistack.kube`, which is where the mechanics live, rather than
reimplemented here. Those parts are marked `-- glue --`, and they are the
honest cost of the composition: the SDK gets you four layers, and the
decisions between them are yours.
"""
import base64
import json
import os
import secrets
import sys
from functools import lru_cache, partial
from urllib.parse import quote

from multistack import (
    Billing,
    BillingBackend,
    Cache,
    CacheBackend,
    ControlPlane,
    ControlPlaneBackend,
    CNPGOptions,
    Database,
    DatabaseBackend,
    DatabaseConfig,
    Enricher,
    EnricherBackend,
    EnricherOptions,
    Gateway,
    GatewayBackend,
    Inference,
    InferenceBackend,
    MinIOTenant,
    NatsQueueOptions,
    KubePrometheusStackOptions,
    Observability,
    ObservabilityBackend,
    Policy,
    PolicyBackend,
    Portal,
    PortalBackend,
    Queue,
    QueueBackend,
    RKE2Cluster,
    Route,
    RouteBackend,
    RKE2Node,
    Stack,
    Storage,
    StorageBackend,
    Tokenizer,
    TokenizerBackend,
    prompt_secret,
)
from multistack.ingress_gateway import IngressGateway, IngressGatewayBackend
from multistack.ingress_gateway.spec import MetalLBIstioOptions
from multistack.accelerator import (
    Accelerator,
    AcceleratorBackend,
    NODE_FEATURE_LABEL,
)
from multistack.kube import apply, node_name_for, wait_for_job
from multistack.kube import kubectl as _kubectl
from multistack.gateway import ModelGatewayOptions
from multistack.cache import ValkeyOptions
from multistack.policy.spec import RateLimits
from multistack.storage import LonghornOptions
from multistack.backends.minio_client import MinIOBackend
from multistack.backends.rke2_client import RKE2Backend

# ---------------------------------------------------------------- config --
# Your lab, from the environment, so this file needs no editing. The
# defaults are RFC 5737 documentation addresses and a documentation user:
# that range is reserved for exactly this purpose, so nothing here can be
# mistaken for a real host.
SSH_USER = os.environ.get("MULTISTACK_SSH_USER", "ubuntu")
SSH_KEY = os.environ.get("MULTISTACK_SSH_KEY", "~/.ssh/id_ed25519")

# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(
    os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig")
)
# The machines. One server, any number of agents, comma-separated.
SERVER = os.environ.get("MULTISTACK_SERVER", "192.0.2.10")
AGENTS = [a.strip() for a in os.environ.get(
    "MULTISTACK_AGENTS", "192.0.2.11,192.0.2.12,192.0.2.13").split(",") if a.strip()]

# The machine with the accelerator, by IP like every other node here.
# Unset skips the GPU stage entirely, which is the right default: most
# labs have no card, and a cluster without one should not fail to build.
# Not INFERENCE_NODE's default either — on this lab they are different
# machines, because the GPU is a compute capability 6.1 card that vLLM's
# PyTorch build cannot use (see stage_inference's own note below).
GPU_NODE = os.environ.get("MULTISTACK_GPU_NODE", "")

# The GPU node's own SSH credential, when it differs from every other
# node's. Unset falls back to SSH_USER/SSH_KEY, so a lab where every node
# shares one credential -- the common case -- needs neither of these set.
# This lab's own GPU node needs both: a different user and a different
# key than the other three, on a different subnet
# (scratch/rebuild/README.md, where this was worked out the first time
# this cluster was built from bare machines).
GPU_SSH_USER = os.environ.get("MULTISTACK_GPU_SSH_USER", SSH_USER)
GPU_SSH_KEY = os.environ.get("MULTISTACK_GPU_SSH_KEY", SSH_KEY)


def _node(address: str, role: str) -> RKE2Node:
    """-- glue -- the one address that may need GPU_SSH_USER/GPU_SSH_KEY
    instead of the uniform SSH_USER/SSH_KEY every other node gets. Not a
    general per-node override table: one address needing one different
    (user, key) pair is the shape this lab actually has. RKE2Node itself
    already carries a `user`/`ssh_key` per node -- reach for that
    directly if a lab needs more than this.
    """
    if address and address == GPU_NODE:
        return RKE2Node(address=address, user=GPU_SSH_USER, role=role,
                         ssh_key=GPU_SSH_KEY)
    return RKE2Node(address=address, user=SSH_USER, role=role, ssh_key=SSH_KEY)


NODES = [
    _node(SERVER, "server"),
    *[_node(a, "agent") for a in AGENTS],
]

# The node inference is pinned to. Labelled and tainted below so nothing
# else schedules there and vLLM gets the whole machine's CPU. Not the GPU
# node: its card is compute capability 6.1, below the 7.0 floor of every
# PyTorch build vLLM ships, so vLLM there fails with "no kernel image is
# available for execution on the device". CPU on a plain worker is the
# working configuration.
INFERENCE_NODE = os.environ.get("MULTISTACK_INFERENCE_NODE", AGENTS[0])

# The first-party images live in a private GHCR namespace, so every
# namespace that runs one needs its own copy of the pull credential --
# an imagePullSecret is namespace-scoped. Unset skips it entirely, which
# is right for a public package or a mirror that needs no auth.
#
# From the environment rather than a flag, and never printed: a token on
# a command line is readable from /proc, and one in shell history is
# readable forever. A file read into the environment is better still.
GHCR_HOST = os.environ.get("MULTISTACK_GHCR_HOST", "ghcr.io")
GHCR_USER = os.environ.get("MULTISTACK_GHCR_USER", "")
GHCR_TOKEN = os.environ.get("MULTISTACK_GHCR_TOKEN", "")
PULL_SECRET = os.environ.get("MULTISTACK_PULL_SECRET", "ghcr-creds")

# The LAN range MetalLB may hand out, and the node it announces from.
# Unset skips the front door entirely, same as the GPU stage: a lab with
# no spare addresses on its subnet should still build.
#
# Layer-2 announcement means the node answers ARP for an address that is
# not configured on any of its interfaces. On a cloud network that is
# usually filtered — on OpenStack the pool has to be added to
# `allowed_address_pairs` on this node's port, or Neutron drops the ARP
# and the address is assigned, healthy, and unreachable.
ADDRESS_POOL = [a for a in os.environ.get(
    "MULTISTACK_ADDRESS_POOL", "").split(",") if a.strip()]
INGRESS_NODE = os.environ.get("MULTISTACK_INGRESS_NODE", AGENTS[-1] if AGENTS else "")

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
BUCKET = "models"
MODEL_KEY = MODEL.split("/")[-1]          # Qwen2.5-0.5B-Instruct
CREDS_SECRET = "minio-creds"
INFERENCE_NS = "inference"

# The platform layers each land in the namespace their own spec already
# declares — Tokenizer and Policy in `policy`, Gateway in `gateway`, both
# control planes in `control-plane`, both portals in `frontend`, the
# database in `postgres`. So none of those is named here and no stage below passes
# `namespace=`: an example that restates a default teaches a layout the
# SDK does not use, and what this file taught until now was to put every
# service into one flat `platform` namespace.
#
# Where a Secret has to be applied into the same namespace as the release
# that reads it, the namespace is read back off the spec class rather than
# typed out again, so there is still exactly one source for each.
#
# The cache is the deliberate exception. `Cache.DEFAULT_NAMESPACES` still
# says `valkey` -- the implementation's name -- but the platform groups it
# by function alongside everything else, and the VALKEY_URL in both
# control-plane Secrets says `cache`. So this overrides the default rather
# than restating it, and the two want reconciling.
CACHE_NS = "cache"

# One database per service, each its own PostgreSQL cluster: every one of
# these owns its schema and migrates it independently, so none can share
# one. Owner names are part of the contract — they appear in the
# DATABASE_URL each service authenticates with — so they are stated here
# rather than derived. Not just the two control planes: billing is
# database-per-service too (Billing.REQUIRES names "database" the same
# way ControlPlane does), so its row lives here rather than being
# special-cased in stage_billing.
DATABASES = (
    # component,      cluster,     database,               owner
    ("admin",         "admin-pg",  "admin_control_plane",  "admin"),
    ("organization",  "org-pg",    "org_control_plane",    "orguser"),
    ("billing",       "billing-pg", "billing",              "billing"),
)

# The Secret billing's chart reads DATABASE_URL, SERVICE_API_KEY and
# (optionally) the two Stripe credentials from.
BILLING_SECRET = "billing-secrets"

# There is no in-cluster registry, so the portal images are built locally
# and sideloaded into one node's containerd. A pod scheduled anywhere else
# stays ImagePullBackOff, which is why this pin is not optional.
IMAGE_NODE = os.environ.get("MULTISTACK_IMAGE_NODE", AGENTS[-1] if AGENTS else "")

# Gateway API is a set of CRDs, and neither RKE2 nor any chart this SDK
# deploys installs them -- multistack/route/README.md says so, and the
# route driver's check_prerequisites reports it. So the route stage
# applies them, pinned: `standard` is the channel holding Gateway and
# HTTPRoute as v1, and the version is the one the working cluster runs
# rather than whatever `latest` resolves to on the day of a rebuild.
#
# Fetched by the kubectl on *this* machine, not from inside the cluster,
# so the nodes need no internet for it.
GATEWAY_API_VERSION = os.environ.get("MULTISTACK_GATEWAY_API_VERSION", "v1.2.1")
GATEWAY_API_MANIFEST = (
    "https://github.com/kubernetes-sigs/gateway-api/releases/download/"
    f"{GATEWAY_API_VERSION}/standard-install.yaml"
)

# The hostnames each service answers on, behind the one front-door
# address. Hostname routing rather than path prefixes for the portals:
# each is served at `/` and its built asset paths assume it, so a prefix
# would return the SPA's own HTML with a 200 for every asset. The model
# gateway takes both -- a hostname *and* /v1 -- because an API client
# sends an explicit path and a browser does not.
#
# These need DNS (or /etc/hosts) pointing at the front door to be
# reachable; the routes attach either way, which is what makes them worth
# creating before the DNS exists.
ADMIN_HOST = os.environ.get("MULTISTACK_ADMIN_HOST", "admin.example.com")
ORG_HOST = os.environ.get("MULTISTACK_ORG_HOST", "org.example.com")
API_HOST = os.environ.get("MULTISTACK_API_HOST", "api.example.com")

# The queue stage below deploys the event backbone (NATS/JetStream) and
# publishes this automatically when "queue" is in STAGES.
# Set here only to point at an externally-managed one instead, or as the
# value main() falls back to when "queue" is trimmed out of a re-run.
# Left empty either way, the policy stage deploys an always-allow limiter
# deliberately rather than one that looks enforcing and is not.
EVENT_BACKBONE = os.environ.get("MULTISTACK_EVENT_BACKBONE", "")

# Which stages to run. Every stage after the first can run alone — the
# specs below are rebuilt either way, and each backend's create() adopts
# whatever is already live rather than replacing it. Overridable so a
# resumed run needs no edit to this file:
#
#   MULTISTACK_STAGES=weights,inference python3 examples/full_stack.py
STAGES = os.environ.get(
    "MULTISTACK_STAGES",
    "cluster,accelerator,ingress,storage,objectstore,weights,inference,"
    "cache,database,registry,tokenizer,queue,enricher,policy,gateway,"
    "billing,controlplane,portal,route,observability"
).split(",")

# The order stages run in, and what to call them. One list rather than a
# number typed into each `print` below: inserting a stage used to mean
# renumbering every banner after it, and twice now the docstrings and the
# banners disagreed about which number a stage was.
STAGE_LABELS = [
    ("cluster",      "RKE2 cluster"),
    ("accelerator",  "GPU scheduling"),
    ("ingress",      "Front door (MetalLB + Istio)"),
    ("storage",      "Longhorn block storage"),
    ("objectstore",  "MinIO object storage"),
    ("inference",    "vLLM inference"),
    ("cache",        "Valkey cache"),
    ("database",     "CloudNativePG"),
    ("registry",     "Image pull credentials"),
    ("tokenizer",    "Tokenizer"),
    ("queue",        "Event backbone / queue (JetStream)"),
    ("enricher",     "Event enricher (token-count guarantee)"),
    ("policy",       "Rate-limit policies"),
    ("gateway",      "Gateway"),
    ("billing",      "Usage metering / billing"),
    ("controlplane", "Control planes"),
    ("portal",       "Web portals"),
    ("route",        "Routes through the front door (API + both portals)"),
    ("observability", "Observability"),
]


# Which namespaces pull a first-party image, read off the specs rather
# than listed here -- the namespace regrouping already moved these once.
PULL_SECRET_NAMESPACES = sorted({
    Tokenizer.DEFAULT_NAMESPACES["tiktoken"],
    Gateway.DEFAULT_NAMESPACES["modelgateway"],
    *Policy.DEFAULT_NAMESPACES.values(),
    *Enricher.DEFAULT_NAMESPACES.values(),
    *Billing.DEFAULT_NAMESPACES.values(),
    *ControlPlane.DEFAULT_NAMESPACES.values(),
    *Portal.DEFAULT_NAMESPACES.values(),
})


def _banner(stage: str) -> None:
    """`=== 3/15  Front door ===`, with the number derived rather than typed.

    The stage docstrings below carry no number for the same reason:
    STAGE_LABELS is the one place the order is stated, so inserting a
    stage cannot leave a banner and a docstring disagreeing -- which is
    exactly what happened twice before this existed.

    `weights` has no banner of its own -- it is a Job the objectstore
    stage's tenant feeds, not a layer -- so it is absent from the list
    above and does not shift anything.
    """
    names = [name for name, _ in STAGE_LABELS]
    label = dict(STAGE_LABELS)[stage]
    print(f"\n=== {names.index(stage) + 1}/{len(names)}  {label} ===")

# The composition root. Which cluster is stated once, here, and threaded
# into every spec below — so `kubeconfig_path` is not repeated five times,
# and the StorageClass MinIO binds against comes from the Storage spec
# that produced it rather than a hardcoded "longhorn" that goes quietly
# wrong the day the implementation changes.
#
# Not ambient: nothing is read from the environment, and stack.outputs
# shows exactly what will be filled in. A spec built directly still works
# — every other example does that.
stack = Stack(kubeconfig_path=KUBECONFIG)


# ------------------------------------------------------------------ glue --
# Bound to this cluster once, so no call below can forget the kubeconfig.
# These are the SDK's own: running kubectl, applying a manifest, waiting on
# a Job and mapping a node IP to its Kubernetes name are mechanism every
# capability needs, so they live in multistack.kube rather than being
# rewritten per script. What stays glue here is the policy — which node to
# reserve, which bucket, what to call the Secret.
kubectl = partial(_kubectl, KUBECONFIG)
_apply = partial(apply, KUBECONFIG)
node_name = partial(node_name_for, KUBECONFIG)


def _authenticated(url: str, owner: str, password: str) -> str:
    """-- glue -- turn a published `database_url` into one a chart can use.

    Two things that URL deliberately is not.

    `+asyncpg`, because each control plane hands this value straight to
    SQLAlchemy's create_async_engine() and has no scheme fix-up of its
    own — a bare `postgresql://` is refused there as a sync driver, at
    startup, before the first query.

    And authenticated: `database_url` omits the password on purpose, so
    that a spec is a file people can commit. Percent-encoded, because a
    prompted password containing `@` or `/` would otherwise be read as
    part of the host.
    """
    return url.replace(
        "postgresql://", "postgresql+asyncpg://", 1
    ).replace(f"{owner}@", f"{owner}:{quote(password, safe='')}@", 1)


CACHE_AUTH_SECRET = "valkey-auth"
CACHE_AUTH_KEY = "valkey-password"


@lru_cache(maxsize=None)
def _cache_password() -> str:
    """-- glue -- the Valkey password, asked for once per run.

    Generated by default: nobody needs to choose this one, and the only
    things that read it are the release itself and the four consumers
    wired below. MULTISTACK_CACHE_PASSWORD keeps it stable across
    separate runs, which is what re-running one stage on its own needs --
    without it, a second run of `cache` would rotate the password and
    leave the consumers from the first run authenticating with the old
    one.

    Cached for the same reason `_org_verify_key` is: three stages need
    this value and a second `generate=True` prompt would hand them
    different ones.
    """
    return prompt_secret(
        "Valkey password (the cache every rate-limit policy counts in)",
        env_var="MULTISTACK_CACHE_PASSWORD",
        generate=True,
    )


def _cache_authenticated(url: str, password: str) -> str:
    """-- glue -- put the password into a published `cache_url`.

    The same shape as `_authenticated` above and for the same reason:
    `Cache.endpoint` omits the credential on purpose, so that a spec is a
    file people can commit, and whatever must authenticate assembles it
    here instead.

    `redis://:password@host` -- Valkey has a password but no username in
    its default ACL, so the user half is empty and the colon stays.
    Percent-encoded, because a generated password containing `@` or `/`
    would otherwise be read as part of the host.
    """
    return url.replace("redis://", f"redis://:{quote(password, safe='')}@", 1)


@lru_cache(maxsize=None)
def _org_verify_key() -> str:
    """-- glue -- the one bearer the gateway presents to the org plane.

    Two releases have to carry this exact same value: it is the
    gateway's `ORG_VERIFY_API_KEY` and the organization control plane's
    `MG_SERVICE_API_KEY`. ADR-026 removed the gateway's static-key auth
    mode, so every /v1 request now asks that plane to resolve the
    caller's bearer into a real per-org key — and if the two values
    disagree the gateway fails closed, which reaches a caller as a 503
    on every single request.

    Deliberately not SERVICE_API_KEY, which the planes use on each
    other: that bearer also opens the admin and org-to-org routes, so
    sharing it would let a compromised gateway escalate into them. This
    one resolves API keys and nothing else.

    Cached because both stages below need it, and a second
    `generate=True` prompt would hand them two different values — the
    exact mismatch described above. MULTISTACK_ORG_VERIFY_API_KEY keeps
    it stable across separate runs, which is what re-running one stage
    on its own needs.
    """
    return prompt_secret(
        "Gateway's bearer against the organization control plane "
        "(its ORG_VERIFY_API_KEY = that plane's MG_SERVICE_API_KEY)",
        env_var="MULTISTACK_ORG_VERIFY_API_KEY",
        generate=True,
    )


@lru_cache(maxsize=None)
def _admin_service_api_key() -> str:
    """-- glue -- admin-control-plane's own SERVICE_API_KEY, reused by
    organization-control-plane and billing.

    One shared value, not per-service: admin-cp's own outbound calls
    to organization-cp carry `Bearer {admin's SERVICE_API_KEY}`
    (admin-control-plane/main.py's org_client), and organization-cp
    checks incoming calls against its *own* configured SERVICE_API_KEY
    (require_service_key) — so the two must be issued the same value,
    or every admin<->org call 401s despite both planes installing
    cleanly and passing their own health probes (neither exercises
    this path). Billing's own `SERVICE_API_KEY` must equal it too: it
    is the outbound bearer admin-cp reuses to notify billing of
    org/plan changes (POST /internal/v1/subscriptions, ADR-032 — see
    examples/billing/install.py). A mismatch there doesn't fail closed
    the way the admin<->org one does; it just makes that best-effort
    notify 401 silently on every plan change.

    Generated, not prompted, the same way each control plane's own
    SERVICE_API_KEY already is — this is a service-to-service credential
    no person ever types. Cached because stage_controlplane (for both
    planes) and stage_billing all need this exact value, and a second
    `secrets.token_urlsafe` call would hand them different ones.
    """
    return secrets.token_urlsafe(24)


def _ensure_namespace(namespace: str) -> None:
    """-- glue -- create a namespace a Secret is about to be applied into.

    Helm creates the namespace it installs into, but these Secrets are
    applied *before* the chart that reads them, so at that point nothing
    has created it yet and the apply fails.
    """
    _apply({
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": namespace},
    })


def _ensure_gateway_api() -> None:
    """-- glue -- install the Gateway API CRDs if they are not there.

    Not a capability: these are cluster-scoped CRDs that several things
    could own, and installing them is a decision about the cluster rather
    than about a route. The SDK deliberately does not install them, and
    the route driver reports their absence -- but reporting it at the
    sixteenth stage of a rebuild, forty minutes in, is too late to be
    useful, and this file is where the cluster-level decisions live.

    Idempotent, and quiet when they already exist: `kubectl apply` on the
    same bundle is a no-op, but skipping it entirely keeps a rebuild from
    needing the internet for a cluster that is already complete.
    """
    existing = kubectl(
        "get", "crd", "httproutes.gateway.networking.k8s.io",
        "--ignore-not-found", "-o", "name", check=False,
    ).strip()
    if existing:
        print("[stack] Gateway API CRDs already installed")
        return

    print(f"[stack] installing Gateway API {GATEWAY_API_VERSION} "
          "(standard channel)")
    kubectl("apply", "-f", GATEWAY_API_MANIFEST)
    # The CRDs have to be established before a route can be applied
    # against them, and apply returns as soon as the objects are created.
    kubectl("wait", "--for=condition=Established", "--timeout=60s",
            "crd/gateways.gateway.networking.k8s.io",
            "crd/httproutes.gateway.networking.k8s.io")
    print("[stack] Gateway API CRDs established")


# ----------------------------------------------------------------- stages --
def stage_cluster() -> str:
    """RKE2 on bare machines."""
    cluster = stack.build(
        RKE2Cluster,
        name="ai-cluster",
        version="v1.32.5+rke2r1",
        nodes=NODES,
        cni="cilium",
        disable_kube_proxy=True,
        # Multi-homed hosts otherwise register on whichever NIC the
        # kubelet picks, which may not be the address declared above.
        pin_node_ip=True,
        # Cilium defaults to 1450. Where the real path MTU is lower, pod
        # traffic to the affected node fails in ways that look like
        # anything but MTU. Measured path MTU in this lab is 1428, so
        # 1428 - 50 (VXLAN overhead) = 1378; 1350 is what the working
        # cluster runs, kept here rather than re-deriving it.
        cilium_mtu=1350,
    )
    RKE2Backend().create(cluster)
    return cluster.kubeconfig_path


def stage_accelerator() -> str | None:
    """GPU scheduling — so that a pod can ask for a card at all.

    Kubernetes does not know a node has a GPU. Until something
    advertises one as an extended resource, `nvidia.com/gpu` is not a
    thing a pod can request, so a GPU workload stays Pending on a
    machine with a perfectly healthy card in it.

    Returns the resource name a pod can now ask for, or None when no GPU
    node was named.
    """
    if not GPU_NODE:
        print("[stack] skipped: no GPU node (set MULTISTACK_GPU_NODE to "
              "the address of the machine with the card)")
        return None

    # -- glue -- the chart's own affinity requires a label that Node
    # Feature Discovery would apply, and this cluster does not run NFD.
    # Labelling a node is a change to the machine rather than to a
    # release, which is why it sits here next to the inference stage's
    # label and taint rather than inside the capability. The driver
    # refuses to install without it, instead of leaving a DaemonSet that
    # wants zero pods and reports no error.
    name = node_name(GPU_NODE)
    kubectl("label", "node", name, f"{NODE_FEATURE_LABEL}=true", "--overwrite")
    print(f"[stack] labelled {name} {NODE_FEATURE_LABEL}=true")

    accelerator = stack.build(
        Accelerator,
        # Pinned: on a cluster where one machine has the card, an
        # unpinned DaemonSet is a pod on every node for the sake of one.
        node_selector={"kubernetes.io/hostname": name},
    )
    resource = AcceleratorBackend().create(accelerator)
    # Deliberately not recorded: this capability publishes nothing. It
    # advertises a property of a node rather than an address, so there is
    # no value for a later spec to fill a field from — see
    # multistack/accelerator/spec.py.
    print(f"[stack] {name} can now be asked for {resource}")
    return resource


def stage_ingress() -> str | None:
    """The front door — one external address for the whole platform.

    MetalLB hands a real LAN address to the Istio ingress gateway's
    Service, because on bare metal no cloud provider does it. Everything
    reachable from outside the cluster goes through this one address
    rather than taking a second one each.

    Returns the external address, or None when no pool was named.
    """
    if not ADDRESS_POOL:
        print("[stack] skipped: no address pool (set "
              "MULTISTACK_ADDRESS_POOL=192.0.2.240-192.0.2.250)")
        return None

    ingress = stack.build(
        IngressGateway,
        address_pool=ADDRESS_POOL,
        # Which nodes may announce the pool. Empty would mean every node,
        # which is only right when every node sits on the pool's subnet.
        node_selector=({"kubernetes.io/hostname": node_name(INGRESS_NODE)}
                       if INGRESS_NODE else {}),
        # Same local-chart escape hatch storage's LonghornOptions.chart
        # uses -- these repos' index.yaml lives on GitHub Pages/GCS, but
        # the tarballs themselves are fetched from
        # release-assets.githubusercontent.com, which is the flaky leg.
        # Unset envs fall back to the chart name, i.e. today's default
        # repo-based resolution.
        options=MetalLBIstioOptions(
            metallb_chart=os.environ.get("MULTISTACK_METALLB_CHART", "metallb"),
            istio_base_chart=os.environ.get("MULTISTACK_ISTIO_BASE_CHART", "base"),
            istiod_chart=os.environ.get("MULTISTACK_ISTIOD_CHART", "istiod"),
            ingress_chart=os.environ.get("MULTISTACK_ISTIO_GATEWAY_CHART", "gateway"),
        ),
    )
    backend = IngressGatewayBackend()
    for warning in backend.check_prerequisites(ingress):
        print(f"[ingress] warning: {warning}")
    endpoint = backend.create(ingress)
    # publishes ingress_gateway_endpoint -- which works because the
    # driver writes the assigned address back onto the spec
    # (drivers/metallb_istio.py). record() skips a None, so the route
    # stage's own guard depends on that write having happened.
    stack.record(ingress)
    print(f"[stack] front door at {endpoint}")
    return endpoint


def stage_storage() -> str:
    """Block storage — the StorageClass RKE2 doesn't ship."""
    storage = stack.build(
        Storage,
        # The implementation. Everything below `type` is the same question
        # asked of any of them; `options` is Longhorn's own.
        type="longhorn",
        # Needs this many schedulable nodes, or volumes stay Degraded.
        replica_count=min(3, len(NODES)),
        default_storage_class=True,
        options=LonghornOptions(
            # Longhorn's own detection reads kubelet's cmdline via pod
            # logs and fails whenever the API server can't reach a node,
            # taking the CSI driver with it. Pinned rather than detected.
            kubelet_root_dir="/var/lib/kubelet",
            # A local `helm pull longhorn/longhorn -d <dir>` .tgz path
            # here, instead of the chart name, points the install at that
            # file directly (HelmRunner's own _local_chart resolution) --
            # useful when the chart repo's release-asset CDN is being
            # slow/flaky and a chart already sitting on disk is faster
            # and more reliable than re-fetching it.
            chart=os.environ.get("MULTISTACK_LONGHORN_CHART", "longhorn"),
        ),
    )
    backend = StorageBackend()
    # Mutates the nodes: open-iscsi + nfs-common. Safe to re-run. Takes the
    # spec as well as the nodes — the spec is what says whose packages.
    backend.install_prerequisites(storage, NODES)
    # EVERY node, not a subset: Longhorn's node components are a
    # DaemonSet and land on nodes whether or not they're listed here.
    storage_class = backend.create(storage, nodes=NODES)
    # After create(), not before: the StorageClass name is knowable from
    # the spec, but publishing it early would let the next layer bind
    # against a class that isn't installed yet.
    stack.record(storage)
    return storage_class


def stage_objectstore() -> MinIOTenant:
    """A MinIO tenant on Longhorn.

    Re-runnable on its own, which the header promises: if the storage
    stage did not run in this process, nothing published a StorageClass,
    so name the one that is already on the cluster. The backend still
    verifies it exists.
    """
    if "storage_class" not in stack:
        stack.provide(storage_class="longhorn")

    tenant = stack.build(
        MinIOTenant,
        name="minio-test",
        namespace="minio-test",
        # 2 x 2 = 4 drives, the minimum for erasure coding.
        servers=2,
        volumes_per_server=2,
        volume_size="20Gi",
        # storage_class comes from the Storage spec recorded above.
        # root_user/root_password unset: generated, and returned below.
    )
    info = MinIOBackend().create(tenant)
    stack.record(tenant)          # publishes the endpoint vLLM reads from

    # -- glue -- vLLM's init container reads the weights as this user, so
    # the credentials have to exist as a Secret in the inference
    # namespace. They never go in a spec.
    #
    # Built as a manifest and piped to stdin, not assembled with
    # `kubectl create secret --from-literal`: that renders the Secret
    # locally, but the keys are in the process arguments while it runs,
    # where any local user can read them out of /proc. Every other
    # credential in this file already went through `apply`; this one was
    # the exception.
    _apply({
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": INFERENCE_NS},
    })
    _apply({
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": CREDS_SECRET, "namespace": INFERENCE_NS},
        "type": "Opaque",
        "data": {
            "AWS_ACCESS_KEY_ID":
                base64.b64encode(info.root_user.encode()).decode(),
            "AWS_SECRET_ACCESS_KEY":
                base64.b64encode(info.root_password.encode()).decode(),
        },
    })
    print(f"[stack] wrote {CREDS_SECRET} to namespace {INFERENCE_NS}")
    # Generated credentials are worth nothing if nobody ever sees them.
    # Printed once, here, because the only other copies are inside two
    # Secrets and there is no way to recover them from the spec.
    print(f"[stack] MinIO root user:     {info.root_user}")
    print(f"[stack] MinIO root password: {info.root_password}")
    print("[stack] store these — they are generated per tenant and not "
          "recoverable from this file")
    # -- glue -- the Secret name is a choice made here, not something the
    # tenant spec knows, so it is published explicitly.
    stack.provide(s3_secret_name=CREDS_SECRET)
    return tenant


def stage_weights(tenant: MinIOTenant) -> None:
    """3b. Put the model in the bucket.

    -- glue -- Object management isn't SDK surface. This runs one Job on
    the cluster that downloads the model and uploads it to MinIO. It uses
    hostNetwork because the pod network here has no route to the
    internet; the whole point of the exercise is that after this runs,
    nothing needs one again.
    """
    script = f"""
set -e
pip install --quiet --no-cache-dir huggingface_hub boto3
# -u, because this script's stdout is a pipe and Python block-buffers a
# pipe. Without it `kubectl logs` shows the download finishing and then
# nothing at all -- a stalled upload and a working one look identical for
# as long as it takes to fill 8KB. That cost a nine-minute wait on
# 2026-09-15 to notice MinIO had no write quorum.
python -u - <<'PY'
import glob, os, urllib3
import boto3
from botocore.config import Config
from huggingface_hub import snapshot_download
import os

urllib3.disable_warnings()
path = snapshot_download(
    "{MODEL}",
    allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
)
s3 = boto3.client(
    "s3",
    endpoint_url="{tenant.endpoint()}",
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    # MinIO is path-style, not the virtual-host addressing boto3 assumes.
    config=Config(s3={{"addressing_style": "path"}}),
    verify=False,   # operator's own CA
)
try:
    s3.create_bucket(Bucket="{BUCKET}")
except Exception as exc:
    print("bucket:", exc)
for f in sorted(glob.glob(os.path.join(path, "*"))):
    if not os.path.isfile(f):
        continue
    key = "{MODEL_KEY}/" + os.path.basename(f)
    size = os.path.getsize(f)
    try:
        # Same name and size already there: a re-run resumes rather than
        # re-uploading a gigabyte.
        if s3.head_object(Bucket="{BUCKET}", Key=key)["ContentLength"] == size:
            print("present", key)
            continue
    except Exception:
        pass
    print("uploading", key, size)
    s3.upload_file(f, "{BUCKET}", key)
print("done")
PY
"""
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "stage-weights", "namespace": INFERENCE_NS},
        "spec": {
            "backoffLimit": 1,
            # Clean itself up an hour after finishing. Without this a
            # completed Job and its pod sit in the namespace forever --
            # `kubectl get pods` then shows a Completed pod next to the
            # running ones, which reads as something that failed to exit.
            "ttlSecondsAfterFinished": 3600,
            "template": {"spec": {
                "restartPolicy": "Never",
                # The pod network has no internet egress; the host does.
                "hostNetwork": True,
                "dnsPolicy": "ClusterFirstWithHostNet",
                "containers": [{
                    "name": "stage",
                    "image": "python:3.11-slim",
                    "command": ["sh", "-c", script],
                    "envFrom": [{"secretRef": {"name": CREDS_SECRET}}],
                }],
            }},
        },
    }
    kubectl("delete", "job", "stage-weights", "-n", INFERENCE_NS,
            "--ignore-not-found")
    _apply(job)
    print(f"[stack] staging {MODEL} into s3://{BUCKET}/{MODEL_KEY} (a few minutes)")
    wait_for_job(KUBECONFIG, "stage-weights", INFERENCE_NS)
    logs = kubectl("logs", "job/stage-weights", "-n", INFERENCE_NS,
                   check=False).strip().splitlines()
    print(f"[stack] staging finished: {logs[-1] if logs else 'no output'}")


def stage_inference(tenant: MinIOTenant) -> str:
    """Serve the model, reading weights from the tenant above."""
    # -- glue -- keep everything else off this node, so vLLM gets its CPU.
    name = node_name(INFERENCE_NODE)
    kubectl("label", "node", name, "multistack.io/workload=inference", "--overwrite")
    kubectl("taint", "node", name, "dedicated=inference:NoSchedule", "--overwrite")
    print(f"[stack] reserved node {name} for inference")

    service = stack.build(
        Inference,
        # The composition: weights come from the tenant created above, so
        # this pod needs no internet access. s3_endpoint_url and
        # s3_secret_name are both filled in from the stack.
        model=f"s3://{BUCKET}/{MODEL_KEY}",
        s3_insecure_tls=tenant.request_auto_cert,

        name="vllm-qwen",
        namespace=INFERENCE_NS,
        device="cpu",
        # None: chosen from the target node's CPU flags, so a bfloat16
        # model isn't emulated in software.
        dtype=None,
        max_model_len=4096,
        # Leaves cores for kubelet/CNI/DaemonSets — asking for all of
        # them leaves the pod Pending.
        cpu_cores=6,
        memory_gb=12,
        kv_cache_gb=4,
        node_selector={"multistack.io/workload": "inference"},
        tolerations=[{
            "key": "dedicated", "operator": "Equal",
            "value": "inference", "effect": "NoSchedule",
        }],
        # No ingress here, so this is what makes the endpoint reachable
        # from outside the cluster and lets the warmup request land.
        host_network=True,
    )
    target = next(n for n in NODES if n.address == INFERENCE_NODE)
    endpoint = InferenceBackend().create(service, nodes=[target])
    # Publishes inference_endpoint. Nothing's FROM_STACK reads it — the
    # gateway's upstream_url is not wired, deliberately — but the stack
    # should still know what it built.
    stack.record(service)
    return endpoint


def stage_cache() -> str:
    """Valkey — the counter store every rate-limit policy reads.

    Returns the *authenticated* URL. Four things read this cache — both
    rate limiters and both control planes — and the URL the Stack
    publishes is deliberately credential-free, so the authenticated one
    is threaded through the return value the same way the database
    stage's is.

    The chart enables auth whether or not it is asked to: `auth.enabled`
    defaults true and `auth.password` empty means "generate a random
    ten-character password", into a Secret nobody here reads. That is the
    worst of the three possible outcomes -- Helm succeeds, the pods go
    Ready, and every consumer gets NOAUTH at its first request, which for
    a rate limiter means failing open. So the password originates here and
    the release is pointed at a Secret holding it.
    """
    # -- glue -- the credential the release reads, applied before the
    # chart that mounts it. Through stdin like every other Secret in this
    # file: `kubectl create secret --from-literal` would put the password
    # in the process arguments, readable from /proc while it runs.
    password = _cache_password()
    _ensure_namespace(CACHE_NS)
    _apply({
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": CACHE_AUTH_SECRET, "namespace": CACHE_NS},
        "type": "Opaque",
        "data": {CACHE_AUTH_KEY:
                 base64.b64encode(password.encode()).decode()},
    })
    print(f"[stack] wrote {CACHE_AUTH_SECRET} to namespace {CACHE_NS}")

    valkey = stack.build(
        Cache,
        name="valkey", namespace=CACHE_NS,
        # No password in the spec. `existingSecret` is a *reference* to
        # the Secret applied above, which is the difference between a spec
        # people can commit and one they cannot.
        options=ValkeyOptions(values={
            "auth": {
                "enabled": True,
                "existingSecret": CACHE_AUTH_SECRET,
                "existingSecretPasswordKey": CACHE_AUTH_KEY,
            },
            # One node, not the chart's default `replication`. These are
            # rate-limit counters: they are rebuilt from the event stream
            # within a window, so a replica set costs three PVCs and a
            # failover story to protect data that is cheap to lose. State
            # it, rather than inheriting a topology nobody chose.
            "architecture": "standalone",
            # AOF, so a restart does not silently reset every counter to
            # zero -- which reads as "nobody has made a request yet" and
            # grants everyone a fresh allowance.
            "primary": {"persistence": {"enabled": True, "size": "8Gi"}},
        }),
    )
    CacheBackend().create(valkey)
    stack.record(valkey)          # publishes cache_url, credential-free
    print(f"[stack] cache at {valkey.endpoint}")
    return _cache_authenticated(valkey.endpoint, password)


def stage_database() -> dict:
    """CloudNativePG — one Postgres cluster per database-owning service
    (both control planes, and billing).

    Returns {component: authenticated DATABASE_URL}. The password is the
    one credential that has to originate here: CloudNativePG reads
    bootstrap.initdb only at bootstrap, so this value is what the role
    is created with. Everything afterwards reads it from the Secret,
    which is why `database_url` carries no password — and why the
    authenticated URL is returned to the caller rather than published.

    Asked for at run time rather than generated, so that whoever runs
    this knows the passwords afterwards. MULTISTACK_ADMIN_DB_PASSWORD,
    MULTISTACK_ORGANIZATION_DB_PASSWORD and MULTISTACK_BILLING_DB_PASSWORD
    keep the script runnable unattended -- ORGANIZATION, not ORG: the
    name is derived from `component` below, and DATABASES spells that
    out in full.
    """
    backend = DatabaseBackend()
    urls = {}

    for component, cluster_name, db_name, owner in DATABASES:
        password = prompt_secret(
            f"PostgreSQL password for role {owner!r} ({cluster_name})",
            env_var=f"MULTISTACK_{component.upper()}_DB_PASSWORD",
            confirm=True,
        )

        database = stack.build(
            Database,
            # namespace omitted: the cnpg implementation's own default
            # is already `postgres`.
            name=cluster_name,
            database=DatabaseConfig(name=db_name, owner=owner,
                                    password=password),
            # Same local-chart escape hatch as storage's LonghornOptions.chart
            # -- the cnpg repo's index.yaml is on GitHub Pages, but the
            # tarball itself comes from release-assets.githubusercontent.com,
            # the same flaky leg that hit Longhorn. A local path already
            # pins a version by definition, and HelmRunner's own
            # _local_chart resolution refuses to combine one with a
            # chart_version -- so drop the operator's default pin
            # (0.29.0) whenever a local path is in play.
            options=CNPGOptions(
                operator_chart=os.environ.get(
                    "MULTISTACK_CNPG_CHART", "cloudnative-pg"),
                operator_chart_version=(
                    None if os.environ.get("MULTISTACK_CNPG_CHART") else "0.29.0"
                ),
            ),
        )
        # The operator is cluster-scoped and installed once. The second
        # call finds the existing release and returns it rather than
        # reinstalling, which is what makes this loop safe.
        backend.create(database)          # the operator
        backend.create_cluster(database)  # this plane's Postgres cluster
        # Recording both leaves `database_url` holding whichever went
        # last — the same "two instances of one capability" caveat the
        # Stack documents for the control planes and portals. It is
        # recorded anyway, because ControlPlane.REQUIRES names
        # "database" and _check_order refuses to build one until
        # something has published it; the per-plane URL is threaded
        # through the return value instead of read back from there.
        stack.record(database)
        urls[component] = _authenticated(database.endpoint, owner, password)
        print(f"[stack] {component} database at {database.endpoint}")

    return urls


def stage_registry() -> None:
    """The credential every first-party image is pulled with.

    From here down the charts pull from a private GHCR namespace, and an
    imagePullSecret is namespace-scoped -- so this is the same Secret
    four times rather than one shared one. Without it the pods stay
    ImagePullBackOff with `unauthorized`, which reads like a missing
    image rather than a missing credential.

    Runs immediately before the first stage that pulls one, rather than
    up with the infrastructure, so the token is held for as short a span
    of the run as possible.
    """
    if not (GHCR_TOKEN and GHCR_USER):
        print("[stack] skipped: no MULTISTACK_GHCR_USER/_TOKEN — assuming "
              "the packages are public, or mirrored somewhere that needs "
              "no credential")
        return

    # dockerconfigjson, assembled here and piped to kubectl on stdin.
    # `kubectl create secret docker-registry --docker-password=...` would
    # be shorter and would put the token in the process arguments, where
    # any local user can read it for as long as the command runs.
    auth = base64.b64encode(f"{GHCR_USER}:{GHCR_TOKEN}".encode()).decode()
    config = json.dumps({"auths": {GHCR_HOST: {"auth": auth}}})
    encoded = base64.b64encode(config.encode()).decode()

    for namespace in PULL_SECRET_NAMESPACES:
        _ensure_namespace(namespace)
        _apply({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": PULL_SECRET, "namespace": namespace},
            "type": "kubernetes.io/dockerconfigjson",
            "data": {".dockerconfigjson": encoded},
        })
    # The token itself is never printed, here or anywhere.
    print(f"[stack] {PULL_SECRET} in {', '.join(PULL_SECRET_NAMESPACES)}")


def stage_tokenizer() -> None:
    """Token counting, for the policy that bills by tokens."""
    tokenizer = stack.build(Tokenizer)
    TokenizerBackend().create(tokenizer)
    stack.record(tokenizer)        # publishes tokenizer_url
    print(f"[stack] tokenizer at {tokenizer.endpoint}")


def stage_queue() -> str:
    """The event backbone (NATS/JetStream) — the raw `GATEWAY_EVENTS` stream
    the gateway publishes to, and the derived `GATEWAY_EVENTS_ENRICHED`
    stream the rate limiters and billing consume instead. `Queue` creates
    both, idempotently, as part of `create()` -- see
    `multistack.queue.drivers.nats.JetStreamQueueDriver`.

    A real capability, unlike the plain `NatsDeployment` this wraps
    (`multistack.nats.deployment` -- Helm/kubectl mechanics only, no
    `CapabilitySpec`): `stack.build(Queue)`/`stack.record()` below is the
    same shape every other stage in this file uses, and it's what lets
    `Gateway` pick up `event_backbone_url` automatically through its own
    `FROM_STACK` rather than needing a manual `stack.provide()` the way
    this stage used to before `Queue` existed.

    `namespace` defaults to `platform`, not this implementation's own
    chart default (`nats`) -- see `Queue.DEFAULT_NAMESPACES`'s own
    comment: `platform` is where NATS already ran before this capability
    replaced the old hand-deployed one there, and every consumer's
    `event_backbone_url` default still assumes that address. Leaving it
    unset here lines this stage up with that, rather than quietly
    standing up a second, differently-addressed NATS nothing points at.

    Returns the in-cluster client URL, the value `event_backbone_url`
    expects everywhere below. Idempotent: a second run adopts the
    existing Helm release, and stream creation skips one that's already
    there.
    """
    queue = stack.build(
        Queue,
        options=NatsQueueOptions(
            # JetStream needs at least 2 replicas for the stream it
            # keeps. The hand-deployed NATS this replaced ran a single
            # pod with none -- losing that node lost the backbone -- so
            # this is the HA topology the migration was for.
            replicas=max(2, min(3, len(NODES))),
            # Same local-chart escape hatch as storage's LonghornOptions.chart
            # -- the nats repo's index.yaml is on GitHub Pages, but the
            # tarball comes from release-assets.githubusercontent.com, the
            # same flaky leg that hit Longhorn/cnpg/istio.
            chart=os.environ.get("MULTISTACK_NATS_CHART", "nats"),
        ),
    )
    backend = QueueBackend()
    for warning in backend.check_prerequisites(queue):
        print(f"[queue] warning: {warning}")
    endpoint = backend.create(queue)
    stack.record(queue)            # publishes event_backbone_url
    print(f"[stack] queue at {endpoint}")
    for name in backend.list_streams(queue):
        print(f"[stack]   stream {name}")
    return endpoint


def stage_enricher(event_backbone_url: str) -> None:
    """Guarantees a token count on every gateway response event.

    Sits between the raw `GATEWAY_EVENTS` stream the queue stage created
    and everything downstream that needs a token count off it --
    rate-limiter-tpm and billing both consume the derived
    `GATEWAY_EVENTS_ENRICHED` stream this produces instead of calling the
    tokenizer themselves (ADR-030). Falls back to the tokenizer stage's
    own endpoint when a response carries no `usage` block.

    No PROVIDES, and so nothing to `stack.record()`: TPM and billing
    reach this service's output through NATS stream/subject strings
    (Enricher's own module docstring), never over HTTP.
    """
    backbone = {"event_backbone_url": event_backbone_url} if event_backbone_url \
        else {"allow_no_backbone": True}
    if not event_backbone_url:
        print("[stack] no event backbone: enricher deploys enriching "
              "nothing (add queue to MULTISTACK_STAGES, or set "
              "MULTISTACK_EVENT_BACKBONE, to enforce)")

    enricher = stack.build(
        Enricher,
        options=EnricherOptions(tokenizer_url=stack.get("tokenizer_url", "")),
        node_selector=({"kubernetes.io/hostname": node_name(IMAGE_NODE)}
                       if IMAGE_NODE else None),
        **backbone,
    )
    backend = EnricherBackend()
    for warning in backend.check_prerequisites(enricher):
        print(f"[enricher] warning: {warning}")
    endpoint = backend.create(enricher)
    print(f"[stack] enricher at {endpoint}  (health/metrics only)")


def stage_policy(cache_auth_url: str | None, event_backbone_url: str) -> list:
    """Rate limiting — requests per minute, then tokens per minute.

    Both read their counters from the cache recorded above; the tpm one
    also reads the tokenizer, for responses whose upstream reported no
    `usage` block.

    `cache_url` arrives from the Stack credential-free and renders into
    each chart's ConfigMap. `cache_auth_url` carries the password and
    renders into each chart's Secret, which envFrom applies second and so
    wins inside the pod. Two fields rather than one because the ConfigMap
    is readable by anything holding `get configmaps` in this namespace,
    and `get configmaps` is granted far more freely than `get secrets`.
    """
    backend = PolicyBackend()
    endpoints = []

    # Counting is asynchronous: /check only reads counters that a
    # JetStream consumer advances from the gateway's events, over the
    # backbone the queue stage deploys. With none configured this is an
    # always-allow /check — deliberate and stated, rather than a limiter
    # that looks enforcing and is not.
    backbone = {"event_backbone_url": event_backbone_url} if event_backbone_url \
        else {"allow_no_backbone": True}
    if not event_backbone_url:
        print("[stack] no event backbone: policies deploy always-allow "
              "(add queue to MULTISTACK_STAGES, or set "
              "MULTISTACK_EVENT_BACKBONE, to enforce)")

    # None when the cache stage did not run this time, which leaves each
    # chart's ConfigMap value standing -- correct for a Valkey with auth
    # disabled, and a NOAUTH at first request for one without. The guard
    # in main() says so rather than letting it be discovered.
    auth = ({"cache_auth_url": cache_auth_url} if cache_auth_url else {})

    rpm = stack.build(
        Policy, type="rpm",
        limits=RateLimits(user_default=60, model_default=600),
        **auth, **backbone,
    )
    # No tokenizer wiring here: since ADR-030 the fallback lives in the
    # `enricher` service (TOKENIZER_URL), which this stack does not stage
    # (see multistack.enricher / examples/enricher/install.py).
    # tokenizer_url is still published above for whatever eventually
    # consumes it -- by hand, or by that example, until enricher gets a
    # stage here.
    tpm = stack.build(
        Policy, type="tpm",
        limits=RateLimits(user_default=100_000, model_default=1_000_000),
        **auth, **backbone,
    )

    for policy in (rpm, tpm):
        for warning in backend.check_prerequisites(policy):
            print(f"[{policy.type}] warning: {warning}")
        endpoints.append(backend.create(policy))
    stack.record(tpm)              # publishes policy_endpoint
    print(f"[stack] policies at {', '.join(endpoints)}")
    return endpoints


def stage_gateway(upstream: str, policy_endpoints: list) -> str:
    """The authenticated front door, in front of inference."""
    key_secret = "gateway-keys"
    # generate=True: nobody needs to *choose* an API key, so Enter is
    # the expected answer. It is returned and printed once at the end.
    api_key = prompt_secret(
        "Gateway API key",
        env_var="MULTISTACK_GATEWAY_API_KEY",
        generate=True,
    )

    # Read off the spec, not typed out: the Secret has to land in the
    # namespace the release will look for it in, and that namespace is
    # the spec's own default, so naming it twice is how they drift apart.
    namespace = Gateway.DEFAULT_NAMESPACES["modelgateway"]
    _ensure_namespace(namespace)

    # Through stdin, never argv: `kubectl create secret --from-literal`
    # puts the key in the process arguments, readable from /proc.
    _apply({
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": key_secret, "namespace": namespace},
        "type": "Opaque",
        "data": {
            "API_KEY": base64.b64encode(api_key.encode()).decode(),
            # Both keys together, in one apply. `kubectl apply` three-way
            # merges, so applying a subset of the keys later would delete
            # whichever this object already had and this manifest omits.
            "ORG_VERIFY_API_KEY":
                base64.b64encode(_org_verify_key().encode()).decode(),
        },
    })
    stack.provide(api_key_secret=key_secret)

    # Derived from the organization plane's own spec rather than typed
    # out, the same way stage_portal derives its api_upstream. It has to
    # be the *organization* plane — only that one resolves an API key —
    # and a Stack records whichever plane went last without being able to
    # tell them apart, which is exactly why Gateway.FROM_STACK
    # deliberately does not wire this field and the spec requires it.
    org_cp = ControlPlane(
        type="organization", kubeconfig_path=KUBECONFIG,
        existing_secret="unused-for-the-url",
    )
    org_cp_url = os.environ.get("MULTISTACK_ORG_CP_URL", org_cp.endpoint)

    gateway = stack.build(
        Gateway,
        upstream_url=upstream,
        org_cp_internal_url=org_cp_url,
        policy_endpoints=policy_endpoints,
        # closed: if the chain cannot be reached, reject. `open` prefers
        # availability over enforcement — the wrong default for anything
        # that bills.
        policy_fail_mode="closed",
    )
    backend = GatewayBackend()
    for warning in backend.check_prerequisites(gateway):
        print(f"[gateway] warning: {warning}")
    endpoint = backend.create(gateway)
    stack.record(gateway)          # publishes gateway_endpoint
    print(f"[stack] gateway at {endpoint}  (API key: {api_key})")
    print(f"[stack] gateway verifies keys against {org_cp_url}")
    return endpoint


def stage_billing(db_urls: dict, event_backbone_url: str) -> str | None:
    """Usage metering, off the enricher's derived event stream, and
    (optionally) Stripe subscription reporting.

    Needs its own database -- see the `billing` row in DATABASES, folded
    into the database stage the same way the two control planes' rows
    are -- and a SERVICE_API_KEY equal to admin-control-plane's own, the
    bearer that plane reuses to notify this service of org/plan changes
    (see `_admin_service_api_key`).

    Returns None, not a deployed-but-broken service, when the database
    stage didn't produce a `billing` entry this run: Billing.REQUIRES
    names "database", but only *a* database, not specifically this one,
    so stack.build() would happily build a spec pointing at nothing.

    Stripe reporting is genuinely optional (ADR-030/031/032): leave
    MULTISTACK_STRIPE_SECRET_KEY/_WEBHOOK_SECRET unset for usage metering
    with no Stripe integration.
    """
    if "billing" not in db_urls:
        print("[stack] skipped: billing needs its own database, which "
              "only the database stage produces (billing is a row in "
              "this file's own DATABASES)")
        return None

    namespace = Billing.DEFAULT_NAMESPACES["stripe"]
    _ensure_namespace(namespace)

    data = {
        "DATABASE_URL": db_urls["billing"],
        "SERVICE_API_KEY": _admin_service_api_key(),
    }
    stripe_key = os.environ.get("MULTISTACK_STRIPE_SECRET_KEY", "")
    stripe_webhook = os.environ.get("MULTISTACK_STRIPE_WEBHOOK_SECRET", "")
    if stripe_key:
        data["STRIPE_SECRET_KEY"] = stripe_key
    if stripe_webhook:
        data["STRIPE_WEBHOOK_SECRET"] = stripe_webhook
    if not (stripe_key and stripe_webhook):
        print("[stack] no Stripe credentials: billing meters usage but "
              "reports nothing to Stripe (MULTISTACK_STRIPE_SECRET_KEY / "
              "MULTISTACK_STRIPE_WEBHOOK_SECRET)")

    # Through stdin, never argv, same as every other Secret in this file.
    _apply({
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": BILLING_SECRET, "namespace": namespace},
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode()
                 for k, v in data.items()},
    })

    backbone = {"event_backbone_url": event_backbone_url} if event_backbone_url \
        else {"allow_no_backbone": True}
    if not event_backbone_url:
        print("[stack] no event backbone: billing deploys metering "
              "nothing (add queue to MULTISTACK_STAGES, or set "
              "MULTISTACK_EVENT_BACKBONE, to enforce)")

    billing = stack.build(
        Billing,
        existing_secret=BILLING_SECRET,
        node_selector=({"kubernetes.io/hostname": node_name(IMAGE_NODE)}
                       if IMAGE_NODE else None),
        **backbone,
    )
    backend = BillingBackend()
    for warning in backend.check_prerequisites(billing):
        print(f"[billing] warning: {warning}")
    endpoint = backend.create(billing)
    stack.record(billing)          # publishes billing_endpoint
    print(f"[stack] billing at {endpoint}")
    return endpoint


def stage_controlplane(db_urls: dict, cache_auth_url: str) -> None:
    """Both control planes, admin and organization.

    The one place credentials are assembled. The charts need an
    *authenticated* DATABASE_URL, and the `database_url` the stack
    publishes is deliberately credential-free — so the two cannot be
    connected, and the authenticated URL goes into a Secret here.

    Taken per plane from `db_urls` rather than read back off the stack:
    each plane has its own database, and a Stack holding two of one
    capability publishes only whichever was recorded last.
    """
    # Authenticated, and taken from the argument rather than read back
    # off the stack for the same reason DATABASE_URL is: what the Stack
    # publishes has no password in it, and these charts need one.
    cache = cache_auth_url
    # One JWT secret, shared: each plane verifies tokens the other issued,
    # so a mismatch makes every cross-service call fail authentication.
    jwt = prompt_secret(
        "JWT signing secret (shared by both control planes)",
        env_var="MULTISTACK_JWT_SECRET",
        generate=True,
    )

    backend = ControlPlaneBackend()
    for cp_type in ("admin", "organization"):
        name = f"{cp_type}-cp-secrets"
        data = {
            "DATABASE_URL": db_urls[cp_type],
            "VALKEY_URL": cache, "JWT_SECRET": jwt,
            # Generated, not prompted, and deliberately so: this is a
            # service-to-service key no person ever types or needs to
            # know. Asking for it would be noise, and a human-chosen
            # value would be weaker than urandom.
            #
            # One shared value, not per-plane: admin-control-plane's own
            # org_client sends `Bearer {its own SERVICE_API_KEY}` when
            # calling org-control-plane (main.py), and org-control-plane
            # checks incoming /v1 and /internal/v1 calls against its own
            # SERVICE_API_KEY (require_service_key) -- and calls admin
            # back the same way for its own outbound requests
            # (organization-control-plane/main.py). A mismatch here 401s
            # every admin<->org call, which installs cleanly and passes
            # both health probes (neither exercises it) -- the same
            # "cannot be generated independently" billing's own
            # SERVICE_API_KEY already avoids, see
            # _admin_service_api_key's docstring.
            "SERVICE_API_KEY": _admin_service_api_key(),
        }
        if cp_type == "admin":
            data["ADMIN_API_KEY"] = secrets.token_urlsafe(24)
        else:
            # Organization only, the same way ADMIN_API_KEY is admin
            # only: this is the plane the gateway asks to resolve a
            # bearer, so it is the only one that needs to recognise the
            # gateway's. Missing, it installs cleanly, passes both
            # probes, and 500s on the first real /v1 request.
            data["MG_SERVICE_API_KEY"] = _org_verify_key()
        # The release's own default namespace, read back off the spec
        # rather than named again here, and created before the Secret
        # goes into it.
        namespace = ControlPlane.DEFAULT_NAMESPACES[cp_type]
        _ensure_namespace(namespace)
        _apply({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": "Opaque",
            "data": {k: base64.b64encode(v.encode()).decode()
                     for k, v in data.items()},
        })

        control_plane = stack.build(
            ControlPlane, type=cp_type,
            existing_secret=name,
            # Seeding runs on first install and is not concurrency-safe:
            # two pods racing both insert the same plan and one dies on
            # the unique index. Scale up after.
            replicas=1 if cp_type == "admin" else 2,
            # Not in FROM_STACK (unset is a supported "billing not
            # deployed yet" stage, not an error -- see the spec's own
            # comment), so read explicitly rather than left to auto-wire.
            # None when the billing stage didn't run this time, which
            # each service's own settings.py default then falls back
            # from -- see ControlPlane.billing_internal_url's docstring.
            billing_internal_url=stack.get("billing_endpoint"),
        )
        for warning in backend.check_prerequisites(control_plane):
            print(f"[{cp_type}] warning: {warning}")
        endpoint = backend.create(control_plane)
        stack.record(control_plane)    # publishes controlplane_endpoint
        print(f"[stack] {cp_type} control plane at {endpoint}")


def stage_portal() -> None:
    """Both web portals, each proxying to its own control plane."""
    backend = PortalBackend()
    for portal_type in ("admin", "organization"):
        # Derived rather than typed out, so renaming a release cannot
        # leave a portal pointing at nothing. A wrong upstream does not
        # error — it serves the SPA's own HTML with a 200 to every API
        # call, and the browser reports a JSON parse error.
        api = ControlPlane(
            type=portal_type, kubeconfig_path=KUBECONFIG,
            existing_secret="unused-for-the-url",
        )
        portal = stack.build(
            Portal, type=portal_type,
            api_upstream=api.endpoint,
            node_selector=({"kubernetes.io/hostname": node_name(IMAGE_NODE)}
                           if IMAGE_NODE else None),
        )
        for warning in backend.check_prerequisites(portal):
            print(f"[{portal_type}-portal] warning: {warning}")
        endpoint = backend.create(portal)
        stack.record(portal)       # publishes portal_endpoint
        print(f"[stack] {portal_type} portal at {endpoint}")


def stage_route() -> str | None:
    """Route all three services through the front door.

    `ingress` provisions the door and deliberately stops there; this
    attaches services to it. The driver creates the parent Gateway on
    first use and one HTTPRoute per service, so this stage owns no
    lifecycle of its own beyond the CRDs those objects need.

    Three routes, not one: the model gateway for API clients, and one per
    portal. The portals go by hostname rather than path prefix -- each is
    served at `/` and its built asset paths assume it, so `/admin` would
    return the SPA's HTML with a 200 for every asset it then asked for.
    """
    if not stack.outputs.get("ingress_gateway_endpoint"):
        print("[stack] skipped: nothing to route through — the ingress "
              "stage did not run, so there is no front door yet")
        return None

    # Before the first build: the driver's check_prerequisites reports
    # missing CRDs as a warning and create() then fails inside kubectl, so
    # the install belongs ahead of both.
    _ensure_gateway_api()

    # (name, namespace, service, port, hostname, path). Service names are
    # the release names the specs already carry -- `<release>-<chart>` is
    # Helm's own naming, and for the portals the chart directory is
    # `ui/chart`, so the release name stands alone. Deriving beats typing:
    # a wrong backend does not error, it 404s at the door.
    targets = [
        ("model-gateway",
         Gateway.DEFAULT_NAMESPACES["modelgateway"],
         f"{ModelGatewayOptions().release_name}-model-gateway",
         8080, API_HOST, "/v1"),
        ("admin-portal",
         Portal.DEFAULT_NAMESPACES["admin"],
         Portal.OPTIONS_FOR_TYPE["admin"]().release_name,
         80, ADMIN_HOST, "/"),
        ("organization-portal",
         Portal.DEFAULT_NAMESPACES["organization"],
         Portal.OPTIONS_FOR_TYPE["organization"]().release_name,
         80, ORG_HOST, "/"),
    ]

    backend = RouteBackend()
    urls, routes = [], {}
    for name, namespace, service, port, hostname, prefix in targets:
        route = stack.build(
            Route,
            # httproute, not virtualservice: the same spec renders against
            # Istio, Cilium or Envoy Gateway.
            name=name,
            namespace=namespace,
            service=service,
            port=port,
            hostnames=[hostname],
            path_prefix=prefix,
        )
        for problem in backend.check_prerequisites(route):
            print(f"[route] warning: {problem}")
        url = backend.create(route)
        urls.append(url)
        routes[name] = route
        print(f"[stack] {hostname}{prefix if prefix != '/' else ''} "
              f"-> {service}:{port}")

    # One recorded, not three: Route PROVIDES `route_url` and a Stack
    # holding three of one capability publishes whichever went last. The
    # model gateway is the one a later layer would mean, so it is named
    # rather than left to loop order.
    stack.record(routes["model-gateway"])       # publishes route_url
    print(f"[stack] routed {len(urls)} services through the front door; "
          f"they need DNS for {API_HOST}, {ADMIN_HOST} and {ORG_HOST} "
          "pointing at it")
    return urls[0]


def stage_observability() -> str:
    """Prometheus, Alertmanager and Grafana.

    Last because it needs the StorageClass, and because everything above
    is worth watching.
    """
    observability = stack.build(
        Observability,
        # Same local-chart escape hatch as storage's LonghornOptions.chart
        # -- prometheus-community's index.yaml is on GitHub Pages, but the
        # tarball comes from release-assets.githubusercontent.com, the
        # same flaky leg that hit Longhorn/cnpg/istio/nats.
        options=KubePrometheusStackOptions(
            chart=os.environ.get(
                "MULTISTACK_KUBE_PROMETHEUS_STACK_CHART", "kube-prometheus-stack"),
        ),
    )
    endpoint = ObservabilityBackend().create(observability)
    stack.record(observability)
    print(f"[stack] grafana at {endpoint}")
    return endpoint


def main() -> int:
    if "cluster" in STAGES:
        _banner("cluster")
        stage_cluster()
    accelerator_resource = None
    if "accelerator" in STAGES:
        _banner("accelerator")
        accelerator_resource = stage_accelerator()

    ingress_endpoint = None
    if "ingress" in STAGES:
        _banner("ingress")
        ingress_endpoint = stage_ingress()

    if "storage" in STAGES:
        _banner("storage")
        stage_storage()

    tenant = None
    if "objectstore" in STAGES:
        _banner("objectstore")
        tenant = stage_objectstore()
    if tenant is None:
        # Later stages re-run alone: rebuild the spec, and create() adopts
        # the live tenant's existing credentials rather than rotating them.
        # Built directly rather than through the stack — nothing recorded
        # a StorageClass this run, and the cluster already has one.
        tenant = MinIOTenant(
            kubeconfig_path=KUBECONFIG, name="minio-test",
            namespace="minio-test", servers=2, volumes_per_server=2,
            volume_size="20Gi", storage_class="longhorn",
        )
        stack.record(tenant).provide(s3_secret_name=CREDS_SECRET)

    if "weights" in STAGES:
        stage_weights(tenant)

    endpoint = None
    if "inference" in STAGES:
        _banner("inference")
        endpoint = stage_inference(tenant)

    # -- the platform on top of it ---------------------------------------
    cache_auth_url = None
    if "cache" in STAGES:
        _banner("cache")
        cache_auth_url = stage_cache()
    if cache_auth_url is None:
        # Re-running a later stage alone: the cache is already up, so
        # rebuild the URL rather than redeploying it. _cache_password()
        # reads MULTISTACK_CACHE_PASSWORD, and prompts when it is unset —
        # which is the right behaviour, because the alternative is wiring
        # four consumers to a password that is merely plausible.
        if {"policy", "controlplane"} & set(STAGES):
            live = Cache(kubeconfig_path=KUBECONFIG, name="valkey",
                         namespace=CACHE_NS)
            stack.record(live)     # publishes cache_url, credential-free
            cache_auth_url = _cache_authenticated(
                live.endpoint, _cache_password())
            print(f"[stack] cache assumed live at {live.endpoint}")

    db_urls = None
    if "database" in STAGES:
        _banner("database")
        db_urls = stage_database()

    if "registry" in STAGES:
        _banner("registry")
        stage_registry()

    if "tokenizer" in STAGES:
        _banner("tokenizer")
        stage_tokenizer()

    event_backbone_url = EVENT_BACKBONE
    if "queue" in STAGES:
        _banner("queue")
        event_backbone_url = stage_queue()
    elif not event_backbone_url and (
        {"policy", "gateway", "enricher", "billing"} & set(STAGES)
    ):
        # Re-running a later stage alone: the queue is already up, so
        # point at it rather than redeploying it -- the same "assumed
        # live" pattern as the cache branch above. `Queue.endpoint` is
        # the same in-cluster DNS name stage_queue's own driver computes;
        # unset kubeconfig-only construction picks up
        # Queue.DEFAULT_NAMESPACES["nats"] ("platform"), the same
        # namespace stage_queue itself defaults to.
        live = Queue(kubeconfig_path=KUBECONFIG)
        event_backbone_url = live.endpoint
        print(f"[stack] event backbone assumed live at {event_backbone_url}")
    if event_backbone_url and "queue" not in STAGES:
        # stage_queue() already did this via stack.record(queue) when it
        # ran. Gateway reads this straight off the Stack via its own
        # FROM_STACK -- stage_gateway below passes nothing for it
        # explicitly. Policy, enricher and billing still take it as an
        # argument because an empty backbone means
        # `allow_no_backbone=True`, which FROM_STACK has no way to derive.
        stack.provide(event_backbone_url=event_backbone_url)

    if "enricher" in STAGES:
        _banner("enricher")
        stage_enricher(event_backbone_url)

    policy_endpoints = []
    if "policy" in STAGES:
        _banner("policy")
        policy_endpoints = stage_policy(cache_auth_url, event_backbone_url)

    gateway_endpoint = None
    if "gateway" in STAGES:
        _banner("gateway")
        # Without the inference stage this run, fall back to the address
        # that stage would have produced — the gateway proxies whatever is
        # actually serving, not only what this process created.
        upstream = endpoint or os.environ.get(
            "MULTISTACK_INFERENCE_ENDPOINT",
            f"http://vllm-qwen.{INFERENCE_NS}.svc.cluster.local:8000",
        )
        gateway_endpoint = stage_gateway(upstream, policy_endpoints)

    billing_endpoint = None
    if "billing" in STAGES:
        _banner("billing")
        if db_urls is None:
            print("[stack] skipped: billing needs its own authenticated "
                  "DATABASE_URL, which only the database stage produces. "
                  "Re-run with database in MULTISTACK_STAGES, with "
                  "MULTISTACK_BILLING_DB_PASSWORD set to the existing "
                  "password.")
        else:
            billing_endpoint = stage_billing(db_urls, event_backbone_url)

    if "controlplane" in STAGES:
        _banner("controlplane")
        if db_urls is None:
            print("[stack] skipped: the control plane Secrets need each "
                  "plane's own authenticated DATABASE_URL, which only the "
                  "database stage produces. Re-run with database in "
                  "MULTISTACK_STAGES, with MULTISTACK_ADMIN_DB_PASSWORD "
                  "and MULTISTACK_ORGANIZATION_DB_PASSWORD set to the "
                  "existing passwords.")
        else:
            stage_controlplane(db_urls, cache_auth_url)

    if "portal" in STAGES:
        _banner("portal")
        stage_portal()

    route_url = None
    if "route" in STAGES:
        _banner("route")
        route_url = stage_route()

    grafana = None
    if "observability" in STAGES:
        _banner("observability")
        grafana = stage_observability()

    print("\n" + "=" * 60)
    print("Stack ready.")
    print(f"  cluster     kubectl --kubeconfig {KUBECONFIG} get nodes")
    print(f"  storage     kubectl --kubeconfig {KUBECONFIG} get sc")
    if accelerator_resource:
        print(f"  accelerator {accelerator_resource} schedulable on {GPU_NODE}")
    print(f"  objects     s3://{BUCKET}/{MODEL_KEY} at {tenant.endpoint()}")
    if endpoint:
        print(f"  inference   {endpoint}")
    if event_backbone_url:
        print(f"  event bus   {event_backbone_url}")
    if gateway_endpoint:
        print(f"  gateway     {gateway_endpoint}")
    if billing_endpoint:
        print(f"  billing     {billing_endpoint}")
    if ingress_endpoint:
        print(f"  front door  {ingress_endpoint}")
    if route_url:
        print(f"  routed      {route_url}")
    if grafana:
        print(f"  grafana     {grafana}")
    print(f"\n  published   {sorted(stack.outputs)}")
    if endpoint:
        print(f"""
Try:
  curl {endpoint}/v1/chat/completions \\
    -H 'Content-Type: application/json' \\
    -d '{{"model":"{MODEL_KEY}","messages":[{{"role":"user","content":"hi"}}],"max_tokens":32}}'""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
