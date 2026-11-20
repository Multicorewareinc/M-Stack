"""
Build the whole stack, bare machines to a served model, in one run.

This is the composition the per-component examples only hint at:

    RKE2       a cluster on bare machines
      -> Storage     block storage (Longhorn), which RKE2 ships without
      -> SeaweedFS   object storage, on top of that block storage
      -> Inference   a model served from that object storage (vLLM)

Each layer's output is the next one's input. The kubeconfig threads
through all four; Longhorn provides the StorageClass SeaweedFS's PVCs
bind to; SeaweedFS's S3 gateway is where vLLM reads its weights — so the
inference pod never needs internet access at all.

Object storage was MinIO until 2026-09-25: MinIO locked pulls of its own
`minio/minio` image behind an authenticated account (confirmed against
quay.io and Docker Hub, every tag back to mid-2024, all 401), which made
it undeployable on a cluster with no such credentials. SeaweedFS's chart
has no equivalent gate.

Vault sits beside that chain rather than in it — it runs right after
storage rather than at the end so that it already exists by the time
the first generated secret needs somewhere to go (see
multistack/vault/client.py's `default_vault_client()`, which SeaweedFS's,
CNPG's and kube-prometheus-stack's own backends now call from inside
their own create()/create_cluster(), and every glue function below that
generates a credential with no owning capability module of its own --
the cache password, the two shared service keys, the JWT secret, the
admin portal's own API key, the gateway's own API key, and Stripe's two
secrets -- calls directly). Silently skipped, not failed, when this
stage didn't run or MULTISTACK_STAGES trims "vault" out — see that
module's own one-warning-per-run behaviour.

    pip install -e .                          # from the repo root
    python3 examples/full_stack.py

DESTRUCTIVE. Stage 1 installs RKE2 on every machine in NODES, so point
it at hosts you are willing to have reformatted. Run it on the first
control-plane node. Needs `ssh`, passwordless key-based SSH to every
node, and a user that is root or has non-interactive `sudo -n`. `kubectl`
and `helm` are not prerequisites: the cluster stage installs both on the
control-plane node and links `~/.kube/config` to MULTISTACK_KUBECONFIG,
so a bare `kubectl` there reaches the same cluster this script did.

Re-running is safe: every stage is idempotent. Trim STAGES to re-run
just part of it — the later stages read what the earlier ones left
behind rather than depending on values passed in memory.

Not everything here is SDK surface. Bucket and object management, node
labels and Secrets are deliberately outside the components, so this
file does them with `kubectl` — from
`multistack.kube`, which is where the mechanics live, rather than
reimplemented here. Those parts are marked `-- glue --`, and they are the
honest cost of the composition: the SDK gets you four layers, and the
decisions between them are yours. Vault's own optional second MetalLB
address/Istio gateway Service (`_vault_ingress_overrides`, only when
`MULTISTACK_VAULT_ADDRESS_POOL` is set) is glue for the same reason:
`ingress_gateway` deliberately models exactly one front door per cluster
(see its own module docstring), so a second address for Vault alone is
this file's problem, not a second capability.
"""

import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from functools import cache, partial
from urllib.parse import quote, urlsplit

from multistack import (
    Billing,
    BillingBackend,
    Cache,
    CacheBackend,
    CNPGOptions,
    ControlPlane,
    ControlPlaneBackend,
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
    KubePrometheusStackOptions,
    Observability,
    ObservabilityBackend,
    Policy,
    PolicyBackend,
    Portal,
    PortalBackend,
    RKE2Cluster,
    RKE2Node,
    Route,
    RouteBackend,
    SeaweedFSCluster,
    Stack,
    Storage,
    StorageBackend,
    Tokenizer,
    TokenizerBackend,
    Vault,
    VaultBackend,
    VaultOptions,
    prompt_secret,
)
from multistack.accelerator import (
    NODE_FEATURE_LABEL,
    Accelerator,
    AcceleratorBackend,
)
from multistack.backends.rke2_client import RKE2Backend
from multistack.backends.seaweedfs_client import SeaweedFSBackend
from multistack.cache import ValkeyOptions
from multistack.gateway import ModelGatewayOptions
from multistack.helm import HelmRepository, HelmRunner
from multistack.inference.spec import (
    KServeLLMOptions,
    KServeModelCache,
    KServeOptions,
    KServePlatform,
)
from multistack.ingress_gateway import IngressGateway, IngressGatewayBackend
from multistack.ingress_gateway.spec import MetalLBIstioOptions
from multistack.kube import apply, node_name_for, wait_for, wait_for_job
from multistack.kube import kubectl as _kubectl
from multistack.policy.spec import RateLimits

# The queue CAPABILITY (provisioning). `multistack.Queue` is the runtime messaging
# facade now, so the spec and its backend are imported from their own modules.
from multistack.queue.registry import QueueBackend
from multistack.queue.spec import NackOptions, NatsQueueOptions, Queue
from multistack.storage import LonghornOptions
from multistack.vault.client import default_vault_client

# ---------------------------------------------------------------- config --
# Your lab, from the environment, so this file needs no editing. The
# defaults are RFC 5737 documentation addresses and a documentation user:
# that range is reserved for exactly this purpose, so nothing here can be
# mistaken for a real host.
SSH_USER = os.environ.get("MULTISTACK_SSH_USER", "ubuntu")
SSH_KEY = os.environ.get("MULTISTACK_SSH_KEY", "~/.ssh/id_ed25519")

# Not /tmp: that does not survive a reboot, and losing the kubeconfig
# leaves you with a cluster you cannot reach and no obvious reason why.
KUBECONFIG = os.path.expanduser(os.environ.get("MULTISTACK_KUBECONFIG", "~/.multistack/kubeconfig"))
# What the cluster stage names the cluster, and what a later run uses to
# find its saved state.
CLUSTER_NAME = "ai-cluster"
# The machines. One server, any number of agents, comma-separated.
SERVER = os.environ.get("MULTISTACK_SERVER", "192.0.2.10")
AGENTS = [
    a.strip()
    for a in os.environ.get("MULTISTACK_AGENTS", "192.0.2.11,192.0.2.12,192.0.2.13").split(",")
    if a.strip()
]

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


# Per-node SSH credentials for any node that differs from the default,
# as comma-separated `address=user:key` entries, e.g.
#   MULTISTACK_NODE_SSH="192.0.2.12=ubuntu:~/.ssh/id_ed25519"
# An entry here wins over the GPU credential and the default. Added when a
# fifth machine joined this lab with a login of its own: one GPU override
# could no longer describe every node, and a node the stack cannot reach
# is a node `cluster` builds without, then `storage` cannot prepare.
def _parse_node_ssh(raw: str) -> dict[str, tuple[str, str]]:
    table: dict[str, tuple[str, str]] = {}
    for entry in filter(None, (e.strip() for e in raw.split(","))):
        address, sep, cred = entry.partition("=")
        user, sep2, key = cred.partition(":")
        if not (sep and sep2 and address.strip() and user.strip() and key.strip()):
            raise SystemExit(f"MULTISTACK_NODE_SSH entry {entry!r} is not address=user:key.")
        table[address.strip()] = (user.strip(), key.strip())
    return table


NODE_SSH = _parse_node_ssh(os.environ.get("MULTISTACK_NODE_SSH", ""))


def _node(address: str, role: str) -> RKE2Node:
    """-- glue -- which SSH credential each node gets: its own entry in
    MULTISTACK_NODE_SSH if it has one, then GPU_SSH_USER/GPU_SSH_KEY for
    the GPU node, then the uniform SSH_USER/SSH_KEY.
    """
    if address in NODE_SSH:
        user, key = NODE_SSH[address]
        return RKE2Node(address=address, user=user, role=role, ssh_key=key)
    if address and address == GPU_NODE:
        return RKE2Node(address=address, user=GPU_SSH_USER, role=role, ssh_key=GPU_SSH_KEY)
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
# Falls back to the server, not AGENTS[0]: a single-node cluster has no
# agents, and indexing an empty list here failed at import time -- before
# any stage ran, with a traceback naming neither the cluster nor the
# reason. INGRESS_NODE and IMAGE_NODE already guard this way.
INFERENCE_NODE = os.environ.get("MULTISTACK_INFERENCE_NODE", AGENTS[0] if AGENTS else SERVER)

# Every first-party chart defaults to keeping its pods off the control
# plane, which is right whenever there is somewhere else to put them and
# unsatisfiable when there is not: on a single-node cluster the only node
# IS the control plane, so the release installs, Helm reports success, and
# the pod sits Pending with nothing saying why. {} overrides that default
# with "schedule anywhere" -- a deliberate statement, which is why it is
# derived from the node count rather than left to whoever runs this.
SERVICE_AFFINITY = {} if len(NODES) == 1 else None

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


# Where the first-party images are pulled FROM. The charts' own
# values.yaml defaults name a registry too, but a chart default is a
# property of the chart, not of the deployment -- deploying from a fork,
# or from the public repo, must not silently keep pulling someone else's
# images. So the deploying repo decides, the same way the Makefile does:
# derived from `origin` unless overridden.
#
# Empty means "leave every chart's own default alone", which is the right
# behaviour outside a git checkout.
def _derive_image_registry() -> str:
    try:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""
    m = re.match(r"^(?:git@|https://|ssh://git@)github\.com[:/]+([^/]+)/(.+?)(?:\.git)?$", url)
    return f"ghcr.io/{m.group(1).lower()}/{m.group(2).lower()}" if m else ""


IMAGE_REGISTRY = os.environ.get("MULTISTACK_IMAGE_REGISTRY", _derive_image_registry())


def image_repo(name: str):
    """Full image path for a first-party service, or None to leave the
    chart's own default in place."""
    return f"{IMAGE_REGISTRY}/{name}" if IMAGE_REGISTRY else None


# Which tag those images carry. Empty leaves every chart's own appVersion
# in place, which is what every deploy did until now -- and what quietly
# pinned the whole platform to :0.1.0 while CI was building a per-commit
# image for each service that nothing ever deployed.
#
# Same reasoning as IMAGE_REGISTRY above: a tag baked into a chart is a
# property of the chart, and which bytes this deployment runs is a
# property of the deployment. Setting this is what makes "rebuild the
# cluster from scratch" mean one identifiable set of images rather than
# whatever a mutable tag happened to point at that week.
IMAGE_TAG = os.environ.get("MULTISTACK_IMAGE_TAG", "")


def image_tag():
    """Tag for a first-party image, or None to leave the chart's own
    appVersion in place."""
    return IMAGE_TAG or None


# The LAN range MetalLB may hand out, and the node it announces from.
# Unset skips the front door entirely, same as the GPU stage: a lab with
# no spare addresses on its subnet should still build.
#
# Layer-2 announcement means the node answers ARP for an address that is
# not configured on any of its interfaces. On a cloud network that is
# usually filtered — on OpenStack the pool has to be added to
# `allowed_address_pairs` on this node's port, or Neutron drops the ARP
# and the address is assigned, healthy, and unreachable.
ADDRESS_POOL = [a for a in os.environ.get("MULTISTACK_ADDRESS_POOL", "").split(",") if a.strip()]
INGRESS_NODE = os.environ.get("MULTISTACK_INGRESS_NODE", AGENTS[-1] if AGENTS else "")

# An optional *second* MetalLB address, for Vault's UI alone. Unset (the
# default) leaves Vault attached to the platform's own shared front door
# above, through the same parent Gateway/HTTPRoute every other routed
# service uses. Vault holds every other capability's generated secrets
# (multistack/vault/README.md), so the firewall rule an operator wants in
# front of it is not necessarily the one already scoped to the model
# gateway and both portals -- giving it its own address is what lets
# those two rules differ. Set only when that separation is wanted; see
# `_vault_ingress_overrides` for what standing this up actually does.
VAULT_ADDRESS_POOL = [
    a for a in os.environ.get("MULTISTACK_VAULT_ADDRESS_POOL", "").split(",") if a.strip()
]
# Which node announces it. Falls back to the same node the shared front
# door announces from, which is correct whenever every node sits on one
# L2 segment -- the common case. Set this separately only when Vault's
# pool lives on a different subnet than ADDRESS_POOL's.
VAULT_INGRESS_NODE = os.environ.get("MULTISTACK_VAULT_INGRESS_NODE", INGRESS_NODE)

# MULTISTACK_VAULT_USERNAME (default "admin") and MULTISTACK_VAULT_PASSWORD
# (unset = prompt interactively, or generate one non-interactively) are
# not threaded through this file as arguments anywhere below. The vault
# driver reads both straight from os.environ itself
# (multistack/vault/drivers/vault.py's `_ensure_admin_user`, the same
# `prompt_secret(..., env_var=...)` shape stage_cache's own password
# uses) -- there is no VaultOptions field for either, so passing them
# through stage_vault would just be a second name for values the driver
# already resolves on its own.

MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
BUCKET = "models"
MODEL_KEY = MODEL.split("/")[-1]  # Qwen2.5-0.5B-Instruct
CREDS_SECRET = "seaweedfs-creds"
INFERENCE_NS = "inference"

# Which inference implementation serves the model. "vllm" runs the
# container directly, as this script always has; "kserve" hands the same
# model to KServe's InferenceService, which owns the Deployment and -- in
# Knative mode -- scales it; "kserve_llm" hands it to KServe's
# LLMInferenceService, vLLM in a pod template KServe merges over its own
# preset. The capability is the same either way, which is the point:
# only this stage changes, and nothing downstream of it learns which one
# ran. The gateway reads an endpoint; it does not read a `type`.
INFERENCE_TYPE = os.environ.get("MULTISTACK_INFERENCE_TYPE", "vllm")
INFERENCE_TYPES = ("vllm", "kserve", "kserve_llm")
if INFERENCE_TYPE not in INFERENCE_TYPES:
    # Refused here rather than defaulted: every branch below reads
    # `== "kserve"`, so any other value -- a typo, or a type this script
    # did not know -- used to fall through to vLLM and deploy the wrong
    # thing without a word.
    raise SystemExit(
        f"MULTISTACK_INFERENCE_TYPE={INFERENCE_TYPE!r} is not one of {INFERENCE_TYPES}."
    )

# Standard or Knative -- KServe 0.20's names for what it used to call
# RawDeployment and Serverless, both of which it still accepts. Standard
# needs nothing beyond KServe itself; Knative adds Knative Serving and, for
# the readiness check to pass, an
# ingress that can hand out an external address -- a NodePort satisfies
# Knative but leaves KServe's own check waiting, so a lab without a
# LoadBalancer should stay on Standard.
KSERVE_MODE = os.environ.get("MULTISTACK_KSERVE_MODE", "Standard")

# A ClusterServingRuntime that already exists in the cluster. Empty lets
# KServe pick one by model format, which is right for a supported format
# on a supported accelerator and wrong for anything hand-built -- a
# ROCm/gfx runtime, say, which no built-in runtime describes.
KSERVE_RUNTIME = os.environ.get("MULTISTACK_KSERVE_RUNTIME", "")

# Whether this script installs KServe (and cert-manager, and Knative when
# the mode needs it) or expects it already there. Installing is the
# default so that a plain cluster works; set to 0 against a cluster whose
# KServe someone else owns.
KSERVE_INSTALL = os.environ.get("MULTISTACK_KSERVE_INSTALL", "1") != "0"

# Where the LLMInferenceService reads its weights from. "node" keeps a
# copy on the inference node's own disk (KServe's LocalModelCache, at
# /models), so a restart mounts it instead of pulling from SeaweedFS
# again; "none" pulls on every start. kserve_llm only -- the other two
# branches are unchanged.
#
# The cache is the inference node and only it: KServe pins a pod to the
# nodes holding its model, so a cache anywhere else leaves the pod
# Pending. Its size is what KServe reserves there, not what the model
# uses; the SDK's 200 default is more than the inference node's whole free disk.
MODEL_CACHE = os.environ.get("MULTISTACK_MODEL_CACHE", "node")
if MODEL_CACHE not in ("node", "none"):
    raise SystemExit(f"MULTISTACK_MODEL_CACHE={MODEL_CACHE!r} is not one of ('node', 'none').")
MODEL_CACHE_GB = int(os.environ.get("MULTISTACK_MODEL_CACHE_GB", "50"))

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
# DATABASE_URL_* each service authenticates with — so they are stated here
# rather than derived. Not just the two control planes: billing is
# database-per-service too (Billing.REQUIRES names "database" the same
# way ControlPlane does), so its row lives here rather than being
# special-cased in stage_billing.
DATABASES = (
    # component,      cluster,     database,               owner
    ("admin", "admin-pg", "admin_control_plane", "admin"),
    ("organization", "org-pg", "org_control_plane", "orguser"),
    ("billing", "billing-pg", "billing", "billing"),
)

# The Secret billing's chart reads DATABASE_URL_BILLING, SERVICE_API_KEY and
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

# The operator-facing consoles. Vault's own route is created by the vault
# stage rather than stage_route (it does not require the ingress
# capability), but its hostname belongs here with the others.
#
# Vault's matters for a second reason: `Vault.hostnames` defaults to
# empty, and an HTTPRoute with no hostnames matches *every* host on the
# Gateway. So until 2026-10-12 the bare front-door address -- and any
# hostname nobody had routed -- fell through to Vault's UI rather than
# 404ing. The portals were unaffected (a route with a matching hostname
# beats one without), but "unrouted name reaches the secret store" is not
# a default worth keeping.
VAULT_HOST = os.environ.get("MULTISTACK_VAULT_HOST", "vault.example.com")
GRAFANA_HOST = os.environ.get("MULTISTACK_GRAFANA_HOST", "grafana.example.com")
PROMETHEUS_HOST = os.environ.get("MULTISTACK_PROMETHEUS_HOST", "prometheus.example.com")
ALERTMANAGER_HOST = os.environ.get("MULTISTACK_ALERTMANAGER_HOST", "alertmanager.example.com")

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
    "cluster,accelerator,ingress,storage,vault,objectstore,weights,inference,"
    "cache,database,registry,tokenizer,queue,enricher,policy,gateway,"
    "billing,controlplane,portal,route,observability,vllm_metrics",
).split(",")

# `route` attaches services to the front door, and reads its address off
# what `ingress` published in this same run -- so a resume like
# MULTISTACK_STAGES=database,...,route used to skip routing entirely
# ("nothing to route through") against a cluster whose front door was up
# all along. Bringing `ingress` along costs nothing: its create() adopts
# the live MetalLB + Istio install rather than replacing it.
if "route" in STAGES and "ingress" not in STAGES:
    STAGES.append("ingress")

# The order stages run in, and what to call them. One list rather than a
# number typed into each `print` below: inserting a stage used to mean
# renumbering every banner after it, and twice now the docstrings and the
# banners disagreed about which number a stage was.
STAGE_LABELS = [
    ("cluster", "RKE2 cluster"),
    ("accelerator", "GPU scheduling"),
    ("ingress", "Front door (MetalLB + Istio)"),
    ("storage", "Longhorn block storage"),
    ("vault", "HashiCorp Vault (secrets)"),
    ("objectstore", "SeaweedFS object storage"),
    ("inference", "Inference (vLLM or KServe)"),
    ("cache", "Valkey cache"),
    ("database", "CloudNativePG"),
    ("registry", "Image pull credentials"),
    ("tokenizer", "Tokenizer"),
    ("queue", "Event backbone / queue (JetStream)"),
    ("enricher", "Event enricher (token-count guarantee)"),
    ("policy", "Rate-limit policies"),
    ("gateway", "Gateway"),
    ("billing", "Usage metering / billing"),
    ("controlplane", "Control planes"),
    ("portal", "Web portals"),
    ("route", "Routes through the front door (API + both portals)"),
    ("observability", "Observability"),
    ("vllm_metrics", "vLLM metrics (Prometheus ServiceMonitor)"),
]


# Which namespaces pull a first-party image, read off the specs rather
# than listed here -- the namespace regrouping already moved these once.
PULL_SECRET_NAMESPACES = sorted(
    {
        Tokenizer.DEFAULT_NAMESPACES["tiktoken"],
        Gateway.DEFAULT_NAMESPACES["modelgateway"],
        *Policy.DEFAULT_NAMESPACES.values(),
        *Enricher.DEFAULT_NAMESPACES.values(),
        *Billing.DEFAULT_NAMESPACES.values(),
        *ControlPlane.DEFAULT_NAMESPACES.values(),
        *Portal.DEFAULT_NAMESPACES.values(),
    }
)


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
# and the StorageClass SeaweedFS binds against comes from the Storage spec
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
    return url.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
        f"{owner}@", f"{owner}:{quote(password, safe='')}@", 1
    )


CACHE_AUTH_SECRET = "valkey-auth"
CACHE_AUTH_KEY = "valkey-password"


@cache
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

    No owning capability module: Cache's own backend never sees this
    raw value (the chart reads it from `CACHE_AUTH_SECRET`, applied
    below as a Secret *reference*, not a spec field -- see
    `stage_cache`'s own docstring), so it is pushed into Vault here,
    glue-level, the moment it's minted, rather than from
    `CacheBackend.create()`.
    """
    password = prompt_secret(
        "Valkey password (the cache every rate-limit policy counts in)",
        env_var="MULTISTACK_CACHE_PASSWORD",
        generate=True,
    )
    # Recorded against this release's own tracked name (Cache.name,
    # "valkey" below) -- the same name @track_create("cache", ...)
    # records it under (multistack/cache/registry.py).
    default_vault_client().store_secret(
        "cache/valkey",
        {"value": password},
        deployment_name="valkey",
    )
    return password


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


@cache
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

    No owning capability module to call `store_secret()` from -- this
    bearer is glue between two charts' Secrets, generated here and
    nowhere else (see the module docstring's note on `_admin_service_
    api_key`), so storing it in Vault happens here too, once, the same
    moment it's minted.
    """
    key = prompt_secret(
        "Gateway's bearer against the organization control plane "
        "(its ORG_VERIFY_API_KEY = that plane's MG_SERVICE_API_KEY)",
        env_var="MULTISTACK_ORG_VERIFY_API_KEY",
        generate=True,
    )
    # Recorded against the gateway's own tracked name ("modelgateway",
    # Gateway.type) -- it is the bearer the gateway itself presents, even
    # though organization-control-plane carries the matching value too.
    default_vault_client().store_secret(
        "shared/org-verify-key",
        {"value": key},
        deployment_name="modelgateway",
    )
    return key


@cache
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

    No owning capability module, same as `_org_verify_key` above: three
    services share this value through their own Secrets, not through
    any spec field, so this is also where it is pushed into Vault.
    """
    key = secrets.token_urlsafe(24)
    # Recorded against the admin control plane's own tracked name
    # ("admin", ControlPlane.type) -- it is admin-control-plane's own
    # SERVICE_API_KEY, reused rather than independently issued by
    # organization-control-plane and billing (see the docstring above).
    default_vault_client().store_secret(
        "shared/admin-service-api-key",
        {"value": key},
        deployment_name="admin",
    )
    return key


def _ensure_cp_tools() -> None:
    """-- glue -- `kubectl` and `helm`, for a run that skips the cluster
    stage.

    The cluster stage leaves both on the control-plane node, along with
    `~/.kube/config` linked to KUBECONFIG — RKE2Backend does that as part
    of create(). A run that starts later (re-running from `storage`, say)
    on a cluster built before it did would otherwise fail at the first
    stage that shells out, as a bare "not found". So that run asks the
    backend for the same step, which touches nothing but the tooling.
    """
    if "cluster" in STAGES:
        return
    if shutil.which("kubectl") and shutil.which("helm"):
        return
    print("[stack] kubectl/helm not on PATH -- preparing the control-plane node")
    RKE2Backend().prepare_control_plane(CLUSTER_NAME)


def _ensure_namespace(namespace: str) -> None:
    """-- glue -- create a namespace a Secret is about to be applied into.

    Helm creates the namespace it installs into, but these Secrets are
    applied *before* the chart that reads them, so at that point nothing
    has created it yet and the apply fails.
    """
    _apply(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace},
        }
    )


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
        "get",
        "crd",
        "httproutes.gateway.networking.k8s.io",
        "--ignore-not-found",
        "-o",
        "name",
        check=False,
    ).strip()
    if existing:
        print("[stack] Gateway API CRDs already installed")
        return

    print(f"[stack] installing Gateway API {GATEWAY_API_VERSION} (standard channel)")
    kubectl("apply", "-f", GATEWAY_API_MANIFEST)
    # The CRDs have to be established before a route can be applied
    # against them, and apply returns as soon as the objects are created.
    kubectl(
        "wait",
        "--for=condition=Established",
        "--timeout=60s",
        "crd/gateways.gateway.networking.k8s.io",
        "crd/httproutes.gateway.networking.k8s.io",
    )
    print("[stack] Gateway API CRDs established")


# Names for Vault's own, optional second front door -- only ever created
# when VAULT_ADDRESS_POOL is set. Distinct from every name
# MetalLBIstioOptions/IngressGateway already use, so the two coexist as
# two unrelated MetalLB pools and two unrelated Services rather than one
# apply colliding with the other's object.
VAULT_POOL_NAME = "vault-pool"
VAULT_L2ADV_NAME = "vault-l2adv"
VAULT_GATEWAY_RELEASE = "vault-ingressgateway"
VAULT_GATEWAY_NAMESPACE = "vault-ingress"
VAULT_PARENT_GATEWAY = "vault-gw"
# Reused rather than retyped: the shared front door's own MetalLB release
# already lives in this namespace (stage_ingress's MetalLBIstioOptions,
# left at its default), and a second IPAddressPool has to land in the
# same namespace MetalLB itself watches or MetalLB never sees it.
METALLB_NAMESPACE = MetalLBIstioOptions().metallb_namespace
# Same repo the shared install (`stage_ingress`) fetches Istio's `gateway`
# chart from. Not imported from `ingress_gateway.drivers.metallb_istio`:
# that module is a driver, and drivers stay private to their own
# capability (see `multistack/ingress_gateway/__init__.py`'s own
# docstring) -- so this is the same URL, restated, rather than a second
# capability reaching into the first one's internals.
_ISTIO_REPOSITORY = HelmRepository(
    name="istio", url="https://istio-release.storage.googleapis.com/charts"
)


def _wait_for_lb_ip(service: str, namespace: str) -> str:
    """-- glue -- polls one Service until MetalLB assigns it an address.

    The same wait `ingress_gateway`'s own driver does for the shared
    front door's Service, restated here for the same reason
    `_ISTIO_REPOSITORY` is above rather than imported from it.
    """
    found: dict = {}

    def has_ip() -> bool:
        ip = kubectl(
            "get",
            "svc",
            service,
            "-n",
            namespace,
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].ip}",
            check=False,
        ).strip()
        if ip:
            found["ip"] = ip
            return True
        return False

    wait_for(has_ip, timeout=300, interval=10, description=f"a MetalLB address for svc/{service}")
    return found["ip"]


def _vault_ingress_overrides() -> dict:
    """-- glue -- Vault's own, optional second MetalLB address.

    `{}` when VAULT_ADDRESS_POOL is unset, which leaves every field this
    would otherwise override at Vault's own class default -- the shared
    `platform-gw` in `istio-ingress`, the same parent every other routed
    service in this file attaches to (today's behaviour, unchanged).

    When it *is* set, this stands up one more MetalLB pool and one more
    Istio `gateway` chart Service to receive it -- not a second call into
    `IngressGatewayBackend`, because that capability deliberately models
    exactly one front door per cluster (see its own module docstring);
    a second address for Vault alone is this file's problem, glued
    together from the same primitives that capability's own driver uses
    (`ingress_gateway/drivers/metallb_istio.py`), not a second instance
    of it. MetalLB and Istio's control plane (istiod/base) are **not**
    reinstalled here -- `stage_ingress` already owns those, and this only
    adds the one thing that differs: a Service pinned to a different
    pool.

    Returns the `Vault(...)` kwargs that point its own HTTPRoute at that
    Service instead of the shared one -- `parent_name`, `parent_namespace`
    and `ingress_service`, the same three fields `VaultDriver._apply_route`
    already reads to decide whether it needs to create a parent Gateway
    (see `multistack/vault/drivers/vault.py`) -- so nothing in the vault
    module itself has to change for this to work.
    """
    if not VAULT_ADDRESS_POOL:
        return {}

    if not ADDRESS_POOL:
        print(
            "[stack] warning: MULTISTACK_VAULT_ADDRESS_POOL is set but "
            "MULTISTACK_ADDRESS_POOL is not -- MetalLB and Istio's "
            "control plane are installed by the ingress stage, so this "
            "needs 'ingress' to have run (this process or a previous "
            "one) before a second Service can attach to them"
        )

    _apply(
        [
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "IPAddressPool",
                "metadata": {"name": VAULT_POOL_NAME, "namespace": METALLB_NAMESPACE},
                # autoAssign=False: without it MetalLB would just as happily
                # hand these addresses to the *next* unrelated LoadBalancer
                # Service that asks for any pool, the same way the shared
                # pool would. Explicit request only, via the Service
                # annotation below.
                "spec": {"addresses": list(VAULT_ADDRESS_POOL), "autoAssign": False},
            },
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "L2Advertisement",
                "metadata": {"name": VAULT_L2ADV_NAME, "namespace": METALLB_NAMESPACE},
                "spec": {
                    "ipAddressPools": [VAULT_POOL_NAME],
                    # Same reasoning as IngressGateway.node_selector's own,
                    # long comment: an announcer with no interface on this
                    # pool's subnet is a Service that looks healthy and
                    # answers no ARP at all.
                    **(
                        {
                            "nodeSelectors": [
                                {
                                    "matchLabels": {
                                        "kubernetes.io/hostname": node_name(VAULT_INGRESS_NODE)
                                    }
                                }
                            ]
                        }
                        if VAULT_INGRESS_NODE
                        else {}
                    ),
                },
            },
        ]
    )

    HelmRunner(
        KUBECONFIG,
        repositories=(_ISTIO_REPOSITORY,),
    ).install_or_upgrade(
        VAULT_GATEWAY_RELEASE,
        chart=os.environ.get("MULTISTACK_ISTIO_GATEWAY_CHART", "gateway"),
        namespace=VAULT_GATEWAY_NAMESPACE,
        values={
            "service": {
                "annotations": {
                    "metallb.universe.tf/address-pool": VAULT_POOL_NAME,
                }
            },
            **(
                {"nodeSelector": {"kubernetes.io/hostname": node_name(VAULT_INGRESS_NODE)}}
                if VAULT_INGRESS_NODE
                else {}
            ),
        },
        # Same escape hatch stage_ingress's own install of this chart
        # uses, and for the same reason -- see MetalLBIstioDriver.create.
        strict_values=False,
    )

    endpoint = _wait_for_lb_ip(VAULT_GATEWAY_RELEASE, VAULT_GATEWAY_NAMESPACE)
    print(f"[stack] vault's own front door at {endpoint} (pool {', '.join(VAULT_ADDRESS_POOL)})")

    return {
        "parent_name": VAULT_PARENT_GATEWAY,
        "parent_namespace": VAULT_GATEWAY_NAMESPACE,
        "ingress_service": VAULT_GATEWAY_RELEASE,
    }


# ----------------------------------------------------------------- stages --
def stage_cluster() -> str:
    """RKE2 on bare machines."""
    cluster = stack.build(
        RKE2Cluster,
        name=CLUSTER_NAME,
        version="v1.36.5+rke2r1",
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
        print(
            "[stack] skipped: no GPU node (set MULTISTACK_GPU_NODE to "
            "the address of the machine with the card)"
        )
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
        print(
            "[stack] skipped: no address pool (set MULTISTACK_ADDRESS_POOL=192.0.2.240-192.0.2.250)"
        )
        return None

    ingress = stack.build(
        IngressGateway,
        address_pool=ADDRESS_POOL,
        # Which nodes may announce the pool. Empty would mean every node,
        # which is only right when every node sits on the pool's subnet.
        node_selector=({"kubernetes.io/hostname": node_name(INGRESS_NODE)} if INGRESS_NODE else {}),
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
        # Unpinned resolves to whatever's newest in the chart repo, which
        # drifts out from under RKE2_VERSION above with no warning until
        # install time -- 1.13.0 requires kubeVersion >=1.34.0-0,
        # incompatible with this file's own v1.32.5+rke2r1 default.
        # 1.12.1 is the newest release still declaring >=1.25.0-0, so
        # it's the default here rather than "latest". A field on Storage
        # itself, not LonghornOptions: every implementation takes this
        # same question (multistack/storage/spec.py).
        chart_version=os.environ.get("MULTISTACK_LONGHORN_CHART_VERSION", "1.12.1"),
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


def stage_vault() -> Vault:
    """HashiCorp Vault — one place for every later stage's generated
    secret to land, instead of a terminal or a `.env` file (see
    `multistack/vault/README.md`'s "how another module should adopt
    this").

    Right after storage and before objectstore, not at the end: the
    SeaweedFS credentials, the database passwords and the Grafana admin
    password are generated in stages that run *after* this one and are
    pushed into Vault from inside their own backends the moment they're
    minted (see `multistack/vault/client.py`'s `default_vault_client()`,
    called from `SeaweedFSBackend.create()`, `CNPGDriver.create_cluster()`
    and `KubePrometheusStackDriver.create()`) — so Vault has to already
    exist for any of them to have somewhere to go. The cache password,
    the two shared service keys, the JWT secret, the admin portal's own
    API key, the gateway's own API key and Stripe's two secrets have no
    owning capability module at all, so those still go into Vault from
    this file's own glue functions instead (`_cache_password`,
    `_org_verify_key`, `_admin_service_api_key`, `stage_controlplane`,
    `stage_gateway`, `stage_billing`) rather than from a backend. Either
    way, the ordering here is what makes any of it possible without
    moving this line again later. It also matches
    `REQUIRES = ("cluster", "storage")` on the nose: Vault's own PVC
    needs a StorageClass exactly the way a
    SeaweedFS tenant's does, so it cannot run any earlier than this
    regardless.

    HA (3-replica Raft) by default, the same `min(3, len(NODES))`
    reasoning `stage_storage`'s own `replica_count` and `stage_queue`'s
    own `replicas` already use for a lab smaller than the 3-node minimum
    this repo's README asks for. Raft additionally needs an *odd* member
    count for a majority to mean anything (`VaultOptions.validate()`
    enforces it) — 2 nodes buys zero fault tolerance over 1, so rather
    than round up to 3 pods this lab's own 2 machines can't actually
    place with any redundancy, it drops to the plain file-backed,
    single-pod shape (`ha_enabled=False`) instead.
    """
    replicas = min(3, len(NODES))
    if replicas % 2 == 0:
        replicas -= 1
    ha_enabled = replicas >= 3

    # Vault creates its own HTTPRoute (see check_prerequisites' own
    # warning below), same as stage_route()'s own targets do -- and same
    # reason that stage calls this first: check_prerequisites reports
    # missing CRDs as a warning, but create() then fails inside kubectl
    # rather than being stopped by it. Idempotent and near-instant once
    # stage_route() (or an earlier run) has already installed them.
    _ensure_gateway_api()

    vault = stack.build(
        Vault,
        options=VaultOptions(ha_enabled=ha_enabled, replicas=replicas),
        # Named, so Vault's route stops being a catch-all -- see
        # VAULT_HOST's own comment. The spec has always supported this;
        # this stage simply never set it.
        hostnames=[VAULT_HOST],
        # {} leaves Vault on the shared front door every other routed
        # service in this file uses — see `_vault_ingress_overrides`.
        **_vault_ingress_overrides(),
    )
    backend = VaultBackend()
    for warning in backend.check_prerequisites(vault):
        print(f"[vault] warning: {warning}")
    result = backend.create(vault)
    stack.record(vault)  # publishes vault_addr
    print(f"[stack] vault UI at {result.ui_url}")
    if result.admin_password:
        # Same one-time rule as every other generated credential in this
        # file (see stage_objectstore's own SeaweedFS print below): the only
        # other copy is inside Vault's own userpass account, and there is
        # no way to recover it from this script afterwards.
        print(f"[stack] vault admin ({result.admin_username}) password: {result.admin_password}")
    if result.newly_initialized:
        # The root token and unseal key(s) are not printed here, or ever
        # again after this call returns — they are already on disk at
        # result.init_file, and that file, not this terminal, is the
        # thing to back up. See README.md's "single most sensitive file
        # in this system" before treating that file as optional.
        print(
            f"[stack] vault initialized for the first time — back up "
            f"{result.init_file} now (mode 0600, holds the unseal "
            "key(s) and the root token, never written to state.db, "
            "never printed by this SDK)"
        )
    return vault


def stage_objectstore() -> SeaweedFSCluster:
    """A SeaweedFS cluster on Longhorn.

    Re-runnable on its own, which the header promises: if the storage
    stage did not run in this process, nothing published a StorageClass,
    so name the one that is already on the cluster. The backend still
    verifies it exists.

    MinIO until 2026-09-25 -- see the module docstring for why. SeaweedFS
    has no operator/tenant split (one release is master+volume+filer+s3
    together), so there is no separate operator-install step the way
    MinIO needed.
    """
    if "storage_class" not in stack:
        stack.provide(storage_class="longhorn")

    cluster = stack.build(
        SeaweedFSCluster,
        name="seaweedfs",
        namespace="seaweedfs",
        master_replicas=1,
        filer_replicas=1,
        # 2 volume replicas, one extra copy per `replication` below --
        # SeaweedFS has no MinIO-style "4 drives minimum" erasure-coding
        # rule, durability comes from `replication`, not replica count.
        #
        # Both derived from the node count, the way Longhorn's
        # replica_count is two stages up, because neither is satisfiable on
        # one node: the volume StatefulSet carries a *required* pod
        # anti-affinity, so a second replica stays Pending with
        #   0/1 nodes are available: 1 node(s) didn't match pod anti-affinity
        # and the chart install then fails on "Ready: 1/2" and rolls back.
        # "001" asks for one extra copy on a *different server*, which a
        # single server cannot provide either -- so it drops to "000",
        # which is what the spec itself defaults to.
        volume_replicas=min(2, len(NODES)),
        volume_size="20Gi",
        replication="001" if len(NODES) > 1 else "000",
        # storage_class comes from the Storage spec recorded above.
        # access_key/secret_key unset: generated, and returned below.
    )
    info = SeaweedFSBackend().create(cluster)
    stack.record(cluster)  # publishes the endpoint vLLM reads from

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
    _apply(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": INFERENCE_NS},
        }
    )
    # -- glue -- the same Secret, read two different ways. vLLM's init
    # container takes the two keys as environment variables, which is all
    # the data below. KServe's storage-initializer reads the *endpoint*
    # from annotations on the Secret instead, and with none it goes to
    # AWS's default endpoint and the model download fails on a bucket
    # that is not there -- which is what happened on 2026-10-09 under
    # MULTISTACK_INFERENCE_TYPE=kserve. The driver warns about this and
    # cannot do more: it does not own the Secret. This stage does.
    #
    # Host:port with no scheme, which is KServe's convention -- the
    # scheme comes from s3-usehttps, and an endpoint carrying `http://`
    # is rejected rather than parsed.
    endpoint = urlsplit(cluster.endpoint())
    _apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": CREDS_SECRET,
                "namespace": INFERENCE_NS,
                "annotations": {
                    "serving.kserve.io/s3-endpoint": endpoint.netloc,
                    "serving.kserve.io/s3-usehttps": "1" if endpoint.scheme == "https" else "0",
                    # In-cluster SeaweedFS has no TLS and no region, but the
                    # AWS SDK the initializer is built on requires a region
                    # to sign with at all.
                    "serving.kserve.io/s3-verifyssl": "1" if endpoint.scheme == "https" else "0",
                    "serving.kserve.io/s3-region": "us-east-1",
                },
            },
            "type": "Opaque",
            "data": {
                "AWS_ACCESS_KEY_ID": base64.b64encode(info.access_key.encode()).decode(),
                "AWS_SECRET_ACCESS_KEY": base64.b64encode(info.secret_key.encode()).decode(),
            },
        }
    )
    print(f"[stack] wrote {CREDS_SECRET} to namespace {INFERENCE_NS}")
    # No longer printed raw: SeaweedFSBackend.create() above already
    # pushed these into Vault at secret/objectstore/<cluster.name> (see
    # multistack/backends/seaweedfs_client.py), the same one-time-rule
    # style stage_vault's own admin-password handling uses. Recoverable
    # from there, or from the config Secret, even when Vault was absent
    # for this run.
    print(
        f"[stack] SeaweedFS credentials stored in Vault at "
        f"secret/objectstore/{cluster.name} (also in {CREDS_SECRET})"
    )
    # -- glue -- the Secret name is a choice made here, not something the
    # cluster spec knows, so it is published explicitly.
    stack.provide(s3_secret_name=CREDS_SECRET)
    return cluster


HF_TOKEN_SECRET = "hf-token"


def stage_weights(cluster: SeaweedFSCluster) -> None:
    """3b. Put the model in the bucket.

    -- glue -- Object management isn't SDK surface. This runs one Job on
    the cluster that downloads the model and uploads it to SeaweedFS. It
    uses hostNetwork because the pod network here has no route to the
    internet; the whole point of the exercise is that after this runs,
    nothing needs one again.

    HF_TOKEN is optional: MODEL above is public, so nothing here needs it
    today, but a gated model (Llama, ...) 401s from snapshot_download
    without one. Applied as a Secret like every other credential in this
    file -- never a literal in the Job's command/args, where /proc would
    expose it -- and pushed to Vault the same way the two shared service
    keys are, since no capability spec owns this value either.
    """
    hf_token = os.environ.get("MULTISTACK_HF_TOKEN", "")
    env_from = [{"secretRef": {"name": CREDS_SECRET}}]
    if hf_token:
        _apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": HF_TOKEN_SECRET, "namespace": INFERENCE_NS},
                "type": "Opaque",
                "data": {"HF_TOKEN": base64.b64encode(hf_token.encode()).decode()},
            }
        )
        env_from.append({"secretRef": {"name": HF_TOKEN_SECRET}})
        # No deployment_name: unlike org-verify-key/admin-service-api-key,
        # this isn't consumed by a tracked component -- it belongs to this
        # Job alone, which (per this function's own docstring) has no
        # state-tracked entry of its own to attribute it to.
        default_vault_client().store_secret("shared/hf-token", {"value": hf_token})
        print("[stack] HF token stored in Vault at secret/shared/hf-token")

    script = f"""
set -e
pip install --quiet --no-cache-dir huggingface_hub boto3
# -u, because this script's stdout is a pipe and Python block-buffers a
# pipe. Without it `kubectl logs` shows the download finishing and then
# nothing at all -- a stalled upload and a working one look identical for
# as long as it takes to fill 8KB. That cost a nine-minute wait on
# 2026-09-15 to notice MinIO had no write quorum -- the same failure mode
# applies here regardless of which object store is behind the endpoint.
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
    # None, not "": huggingface_hub treats an empty string as a real
    # (invalid) token rather than "no token", which 401s a public model
    # too -- os.environ.get's own default already covers HF_TOKEN unset.
    token=os.environ.get("HF_TOKEN") or None,
)
s3 = boto3.client(
    "s3",
    endpoint_url="{cluster.endpoint()}",
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    # SeaweedFS's S3 gateway is path-style, not the virtual-host
    # addressing boto3 assumes.
    config=Config(s3={{"addressing_style": "path"}}),
    verify=False,   # plain HTTP endpoint -- see SeaweedFSCluster.endpoint()
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
        #
        # And then read a byte of it, because `head_object` answers from
        # the filer's catalogue alone. On 2026-10-09 a rebuilt cluster
        # inherited that catalogue from the deployment before it -- the
        # filer's store was a node hostPath, the volumes holding the
        # bytes were not -- so all seven objects were "present", this
        # Job completed green having uploaded nothing, and KServe's
        # storage-initializer then failed every GetObject with 500
        # InternalError. Reading one byte distinguishes "the object is
        # there" from "something once wrote its name down".
        if s3.head_object(Bucket="{BUCKET}", Key=key)["ContentLength"] == size:
            s3.get_object(Bucket="{BUCKET}", Key=key, Range="bytes=0-0")["Body"].read()
            print("present", key)
            continue
    except Exception as exc:
        # Says why, because the two reasons mean very different things: a
        # 404 is an ordinary first run, anything else is an object whose
        # name is in the catalogue and whose bytes are not reachable.
        print("will upload", key, "--", type(exc).__name__, exc)
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
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    # The pod network has no internet egress; the host does.
                    "hostNetwork": True,
                    "dnsPolicy": "ClusterFirstWithHostNet",
                    "containers": [
                        {
                            "name": "stage",
                            "image": "python:3.11-slim",
                            "command": ["sh", "-c", script],
                            "envFrom": env_from,
                        }
                    ],
                }
            },
        },
    }
    kubectl("delete", "job", "stage-weights", "-n", INFERENCE_NS, "--ignore-not-found")
    _apply(job)
    print(f"[stack] staging {MODEL} into s3://{BUCKET}/{MODEL_KEY} (a few minutes)")
    wait_for_job(KUBECONFIG, "stage-weights", INFERENCE_NS)
    logs = (
        kubectl("logs", "job/stage-weights", "-n", INFERENCE_NS, check=False).strip().splitlines()
    )
    print(f"[stack] staging finished: {logs[-1] if logs else 'no output'}")


def _vllm_qwen_kwargs(*, s3_endpoint_url: str, dtype: str | None) -> dict:
    """Every field the deployed vllm-qwen Inference spec needs, shared
    between stage_inference() (the first deploy) and stage_vllm_metrics()
    (a later re-apply, standalone-runnable, that must not silently
    narrow the manifest -- see that function's own docstring for the
    2026-09-25 incident a field-list divergence between the two caused:
    a partial re-apply wiped node_selector/tolerations and swapped the
    model for an un-mirrored one). One shared source, so a future field
    added here reaches both call sites instead of only whichever one
    happened to get edited.

    dtype is the one field that has to differ, not duplicate:
    stage_inference() passes None so the driver auto-detects it from the
    target node's CPU flags (needs `nodes=[target]` at create() time,
    which only that call site has); stage_vllm_metrics() has no node to
    detect from, so it pins the value already resolved there.
    """
    return dict(
        # The composition: weights come from the SeaweedFS cluster, so
        # this pod needs no internet access. s3_insecure_tls left at its
        # default (False): SeaweedFS's endpoint is plain HTTP (see
        # SeaweedFSCluster.endpoint()'s own docstring -- no TLS is
        # modelled, so there is no cert to skip verifying), unlike
        # MinIO's per-tenant auto-cert this replaced.
        model=f"s3://{BUCKET}/{MODEL_KEY}",
        s3_endpoint_url=s3_endpoint_url,
        s3_secret_name=CREDS_SECRET,
        name="vllm-qwen",
        namespace=INFERENCE_NS,
        device="cpu",
        dtype=dtype,
        max_model_len=1024,
        # Leaves cores for kubelet/CNI/DaemonSets — asking for all of
        # them leaves the pod Pending.
        cpu_cores=4,
        # 6Gi OOM-killed this pod on 2026-09-25 (exitCode 137) right after
        # KV-cache allocation finished: 1Gi kv_cache + 1Gi shm (tmpfs,
        # charged to the same cgroup) leaves ~4Gi for three separate
        # processes (APIServer/EngineCore/Worker) doing a float32 upcast
        # plus torch.compile, which doesn't fit. The inference node has 13Gi free,
        # so there's room to give this real headroom.
        memory_gb=12,
        kv_cache_gb=1,
        shm_gb=1,
        node_selector={"multistack.io/workload": "inference"},
        tolerations=[
            {
                "key": "dedicated",
                "operator": "Equal",
                "value": "inference",
                "effect": "NoSchedule",
            }
        ],
        # No ingress here, so this is what makes the endpoint reachable
        # from outside the cluster and lets the warmup request land.
        host_network=True,
    )


def stage_inference(cluster: SeaweedFSCluster) -> str:
    """Serve the model, reading weights from the cluster above.

    Two implementations, chosen by INFERENCE_TYPE. Both are handed the same
    model and the same credentials and both return an endpoint; the stages
    after this one cannot tell which ran, and that substitutability is the
    whole reason `type` lives on the spec rather than in the caller.
    """
    if INFERENCE_TYPE == "kserve":
        return _inference_kserve(cluster)
    if INFERENCE_TYPE == "kserve_llm":
        return _inference_kserve(cluster, kwargs=_kserve_llm_qwen_kwargs)
    return _inference_vllm(cluster)


def _inference_vllm(cluster: SeaweedFSCluster) -> str:
    """vLLM, running the server container directly."""
    # -- glue -- keep everything else off this node, so vLLM gets its CPU.
    name = node_name(INFERENCE_NODE)
    kubectl("label", "node", name, "multistack.io/workload=inference", "--overwrite")
    kubectl("taint", "node", name, "dedicated=inference:NoSchedule", "--overwrite")
    print(f"[stack] reserved node {name} for inference")

    service = stack.build(
        Inference,
        # None: chosen from the target node's CPU flags, so a bfloat16
        # model isn't emulated in software.
        **_vllm_qwen_kwargs(s3_endpoint_url=cluster.endpoint(), dtype=None),
    )
    target = next(n for n in NODES if n.address == INFERENCE_NODE)
    endpoint = InferenceBackend().create(service, nodes=[target])
    # Publishes inference_endpoint. Nothing's FROM_STACK reads it — the
    # gateway's upstream_url is not wired, deliberately — but the stack
    # should still know what it built.
    stack.record(service)
    return endpoint


def _inference_kserve(cluster: SeaweedFSCluster, kwargs=None) -> str:
    """KServe, which owns the Deployment and the Service for us.

    The same `Inference` fields the vLLM branch sets mean the same things
    here -- model, device, resources, scheduling -- because they are
    properties of "serve this model", not of vLLM. What differs is who
    reconciles the workload, and that is `options`.

    No host_network: Knative refuses a host-network pod outright, and the
    endpoint comes from the predictor Service instead, which is what
    makes this reachable without one.

    The node reservation is the same as the vLLM branch's, though, and
    for the same reason. "KServe places the predictor itself" was true
    and not good enough: on 2026-10-09 it placed a 4-CPU/12Gi predictor
    on rke2-cp01, because nothing on this cluster taints the control
    plane and nothing in this branch said otherwise. Inference belongs on
    a worker whatever reconciles it, so this branch reserves the node the
    same way and selects on the label it writes -- `node_selector` and
    `tolerations` are ordinary `Inference` fields that the KServe driver
    puts straight on the predictor.
    """
    name = node_name(INFERENCE_NODE)
    kubectl("label", "node", name, "multistack.io/workload=inference", "--overwrite")
    # Labelled but not tainted when the node holds a model cache: KServe's
    # cache agent is a DaemonSet with no tolerations (and the SDK adds
    # none), so behind the taint it never runs and the copy the pod waits
    # for is never made. The label alone still keeps inference off the
    # control plane; what goes is "nothing else may schedule here".
    # Untainted explicitly, since an earlier run may have tainted it.
    node_cache = kwargs is not None and MODEL_CACHE == "node"
    if node_cache:
        kubectl("taint", "node", name, "dedicated=inference:NoSchedule-", check=False)
        print(f"[stack] labelled node {name} for inference (untainted: it holds the model cache)")
    else:
        kubectl("taint", "node", name, "dedicated=inference:NoSchedule", "--overwrite")
        print(f"[stack] reserved node {name} for inference")

    service = stack.build(
        Inference,
        **(
            kwargs(s3_endpoint_url=cluster.endpoint(), cache_node=name if node_cache else None)
            if kwargs
            else _kserve_qwen_kwargs(s3_endpoint_url=cluster.endpoint())
        ),
    )
    # nodes= is what lets the driver check the cluster can actually run
    # this -- that the GPU resource is advertised, that a named runtime
    # exists -- before it creates an InferenceService that would otherwise
    # sit un-schedulable with the reason buried in an event.
    endpoint = InferenceBackend().create(service, nodes=NODES)
    # Publishes inference_endpoint, exactly as the vLLM branch does. The
    # endpoint already carries KServe's path prefix, so a caller that
    # appends /v1/... to it reaches the right place without knowing that
    # KServe, rather than vLLM, is behind it.
    stack.record(service)
    print(f"[stack] inference at {endpoint}")
    return endpoint


def _kserve_qwen_kwargs(*, s3_endpoint_url: str) -> dict:
    """Every field the KServe deployment is built from, in one place.

    The KServe counterpart of `_vllm_qwen_kwargs`, and added for the
    reason that one exists: `stage_vllm_metrics` re-applies the
    inference spec to attach a ServiceMonitor, and a spec rebuilt from
    defaults at that call site is a 3-way merge that quietly rewrites
    the running workload. The vLLM branch learned that on 2026-09-25 and
    was fixed by sharing its fields; the KServe branch was never given
    the same treatment, so on 2026-10-09 the metrics stage ignored
    `INFERENCE_TYPE` entirely and stood up a *second*, vLLM deployment
    beside the KServe one -- two servers, same model, 4 CPU and 12Gi
    each.
    """
    return dict(
        type="kserve",
        # Identical to the vLLM branch: weights out of the SeaweedFS
        # cluster above, so the predictor needs no internet access either.
        # s3_insecure_tls stays at its default for the same reason
        # _vllm_qwen_kwargs gives -- SeaweedFS serves plain HTTP, so there
        # is no certificate to skip verifying.
        model=f"s3://{BUCKET}/{MODEL_KEY}",
        s3_endpoint_url=s3_endpoint_url,
        s3_secret_name=CREDS_SECRET,
        name="kserve-qwen",
        namespace=INFERENCE_NS,
        device="cpu",
        dtype=None,
        # Matched to the vLLM branch's tuned values rather than the spec
        # defaults: the same server, the same CPU upcast and the same
        # cgroup accounting that OOM-killed this model at 6Gi on 2026-09-25
        # apply here too, and KServe does not change any of that.
        max_model_len=1024,
        cpu_cores=4,
        memory_gb=12,
        kv_cache_gb=1,
        shm_gb=1,
        # Both halves of the reservation above: the label picks the node,
        # the toleration gets past the taint that keeps everything else
        # off it. Selecting on the workload label rather than the
        # hostname so a cluster can move inference to another node by
        # relabelling.
        node_selector={"multistack.io/workload": "inference"},
        tolerations=[
            {"key": "dedicated", "operator": "Equal", "value": "inference", "effect": "NoSchedule"}
        ],
        options=KServeOptions(
            deployment_mode=KSERVE_MODE,
            # Empty means "let KServe choose by model format". An explicit
            # name is required for a hand-built runtime, which no built-in
            # ClusterServingRuntime describes.
            runtime=KSERVE_RUNTIME or None,
            platform=KServePlatform(
                install=KSERVE_INSTALL,
                # Only the Knative mode needs Knative, and install_platform
                # reads the mode to decide -- so this stays as it is for
                # both and the mode above is the single switch.
                knative_ingress_service_type=os.environ.get(
                    "MULTISTACK_KNATIVE_INGRESS_SERVICE_TYPE"
                )
                or None,
            ),
        ),
    )


def _kserve_llm_qwen_kwargs(*, s3_endpoint_url: str, cache_node: str | None = None) -> dict:
    """Every field the LLMInferenceService deployment is built from.

    Shared by stage_inference and the probes below, for the reason
    `_kserve_qwen_kwargs` gives. The model, credentials, resources and
    scheduling are the KServe branch's own -- they describe serving this
    model on this cluster, not which KServe resource does it.

    No `image`: on CPU the driver supplies the CPU vLLM build and the
    securityContext it needs (the preset's image is CUDA-only, and its
    runAsNonRoot refuses the CPU build). `service_monitor_labels` is left
    out because the kserve_llm type refuses it; see stage_vllm_metrics.

    `cache_node` is the Kubernetes node name that keeps the weights on
    disk (MODEL_CACHE); None reads them from SeaweedFS on every start. A
    name rather than looked up here, because resolving one needs the
    cluster and the probes below build this without one.
    """
    kwargs = _kserve_qwen_kwargs(s3_endpoint_url=s3_endpoint_url)
    cache = (
        KServeModelCache(enabled=True, nodes=[cache_node], size_gb=MODEL_CACHE_GB)
        if cache_node
        else KServeModelCache()
    )
    kwargs.update(
        type="kserve_llm",
        name="llm-qwen",
        model_cache="node" if cache_node else "none",
        # The model's own size, which KServe needs on every cache to refuse
        # one the node has no room for. Qwen2.5-0.5B-Instruct is ~1 GB.
        cache_size_gb=2 if cache_node else None,
        options=KServeLLMOptions(
            platform=KServePlatform(install=KSERVE_INSTALL, model_cache=cache),
        ),
    )
    return kwargs


def _served_model_name() -> str:
    """The model id the inference stage's server answers to.

    Read off the same probe spec as the endpoint, never typed out: it is
    not the same for both implementations. vLLM is told its name and
    answers to the model's basename ("Qwen2.5-0.5B-Instruct"); KServe
    answers to the InferenceService's name ("kserve-qwen"). Keying the
    gateway route on MODEL_KEY advertised the vLLM name for both, and on
    KServe every request through it came back 404.
    """
    return _inference_probe().served_name


def _inference_fallback_endpoint() -> str:
    """Where the inference stage would have published, without running it.

    Built from a spec rather than formatted by hand: the address depends on
    the implementation -- KServe serves from `<name>-predictor` on port 80
    under a path prefix, vLLM from `<name>` on 8000 at the root -- and
    `Inference.endpoint` is the one place that knows the difference. A
    hand-written f-string here was right for vLLM and silently wrong for
    KServe, pointing the gateway at a Service that does not exist.
    """
    return _inference_probe().endpoint


def _inference_probe() -> Inference:
    """A spec with the inference stage's own name and type, built without
    deploying anything -- what the endpoint and the served name are read
    from when a later stage needs them."""
    if INFERENCE_TYPE == "kserve":
        return Inference(
            kubeconfig_path=KUBECONFIG,
            type="kserve",
            name="kserve-qwen",
            namespace=INFERENCE_NS,
            options=KServeOptions(deployment_mode=KSERVE_MODE),
        )
    if INFERENCE_TYPE == "kserve_llm":
        return Inference(
            kubeconfig_path=KUBECONFIG,
            type="kserve_llm",
            name="llm-qwen",
            namespace=INFERENCE_NS,
        )
    return Inference(
        kubeconfig_path=KUBECONFIG,
        name="vllm-qwen",
        namespace=INFERENCE_NS,
    )


def _served_model_name() -> str:
    """The model id this stack's inference answers to.

    Same probe-spec idiom as `_inference_fallback_endpoint` above, and
    for the same reason: the gateway stage may run without the inference
    stage in the same process, so there is no recorded spec to read, and
    `served_name` is a pure function of fields known here.
    """
    if INFERENCE_TYPE == "kserve":
        probe = Inference(
            kubeconfig_path=KUBECONFIG,
            type="kserve",
            name="kserve-qwen",
            namespace=INFERENCE_NS,
            options=KServeOptions(deployment_mode=KSERVE_MODE),
        )
    elif INFERENCE_TYPE == "kserve_llm":
        probe = Inference(
            kubeconfig_path=KUBECONFIG,
            type="kserve_llm",
            name="llm-qwen",
            namespace=INFERENCE_NS,
        )
    else:
        probe = Inference(
            kubeconfig_path=KUBECONFIG,
            name="vllm-qwen",
            namespace=INFERENCE_NS,
            model=f"s3://{BUCKET}/{MODEL_KEY}",
        )
    return probe.served_name


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
    _apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": CACHE_AUTH_SECRET, "namespace": CACHE_NS},
            "type": "Opaque",
            "data": {CACHE_AUTH_KEY: base64.b64encode(password.encode()).decode()},
        }
    )
    print(f"[stack] wrote {CACHE_AUTH_SECRET} to namespace {CACHE_NS}")

    valkey = stack.build(
        Cache,
        name="valkey",
        namespace=CACHE_NS,
        # No password in the spec. `existingSecret` is a *reference* to
        # the Secret applied above, which is the difference between a spec
        # people can commit and one they cannot.
        options=ValkeyOptions(
            values={
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
            }
        ),
    )
    CacheBackend().create(valkey)
    stack.record(valkey)  # publishes cache_url, credential-free
    print(f"[stack] cache at {valkey.endpoint}")
    return _cache_authenticated(valkey.endpoint, password)


def stage_database() -> dict:
    """CloudNativePG — one Postgres cluster per database-owning service
    (both control planes, and billing).

    Returns {component: authenticated database URL}. The password is the
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
            database=DatabaseConfig(name=db_name, owner=owner, password=password),
            # Database has carried an `affinity` field all along, but its
            # default is the same control-plane exclusion the charts use --
            # so a single-node run bootstrapped a Cluster whose initdb Job
            # could never be scheduled, and CNPG reported nothing beyond a
            # Pending pod until the stage's ready_timeout expired.
            affinity=SERVICE_AFFINITY,
            # Same local-chart escape hatch as storage's LonghornOptions.chart
            # -- the cnpg repo's index.yaml is on GitHub Pages, but the
            # tarball itself comes from release-assets.githubusercontent.com,
            # the same flaky leg that hit Longhorn. A local path already
            # pins a version by definition, and HelmRunner's own
            # _local_chart resolution refuses to combine one with a
            # chart_version -- so drop the operator's default pin
            # (0.29.0) whenever a local path is in play.
            options=CNPGOptions(
                operator_chart=os.environ.get("MULTISTACK_CNPG_CHART", "cloudnative-pg"),
                operator_chart_version=(
                    None if os.environ.get("MULTISTACK_CNPG_CHART") else "0.29.0"
                ),
            ),
        )
        # The operator is cluster-scoped and installed once. The second
        # call finds the existing release and returns it rather than
        # reinstalling, which is what makes this loop safe.
        backend.create(database)  # the operator
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
        print(
            "[stack] skipped: no MULTISTACK_GHCR_USER/_TOKEN — assuming "
            "the packages are public, or mirrored somewhere that needs "
            "no credential"
        )
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
        _apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": PULL_SECRET, "namespace": namespace},
                "type": "kubernetes.io/dockerconfigjson",
                "data": {".dockerconfigjson": encoded},
            }
        )
    # The token itself is never printed, here or anywhere.
    print(f"[stack] {PULL_SECRET} in {', '.join(PULL_SECRET_NAMESPACES)}")


def stage_tokenizer() -> None:
    """Token counting, for the policy that bills by tokens."""
    tokenizer = stack.build(
        Tokenizer,
        affinity=SERVICE_AFFINITY,
        options={"image_repository": image_repo("tokenizer"), "image_tag": image_tag()},
    )
    TokenizerBackend().create(tokenizer)
    stack.record(tokenizer)  # publishes tokenizer_url
    print(f"[stack] tokenizer at {tokenizer.endpoint}")


def stage_queue() -> str:
    """The event backbone (NATS/JetStream) — the raw `GATEWAY_EVENTS` stream
    the gateway publishes to, and the derived `GATEWAY_EVENTS_ENRICHED`
    stream the rate limiters and billing consume instead.

    One install, because the capability now carries both halves.
    `Queue.create()` deploys the NATS servers *and* installs NACK, the
    controller that owns the streams and consumers as Kubernetes
    resources. The servers alone are not the backbone: the low-level
    `NatsDeploymentManager.create` says so in those words -- "This
    operation deploys only NATS infrastructure. It does not create
    JetStream Streams or Consumers" -- and this docstring nonetheless
    claimed `create()` made both until 2026-10-09, when the backbone came
    up with no `GATEWAY_EVENTS` at all and every consumer that binds by
    name failed against it. The controller was wired up here by hand out
    of `multistack.queue.drivers.nats.nack` after that; it travels with
    the capability now, as `options.nack`, so no example has to reach
    past a backend to get a backbone that works.

    A real capability, unlike the plain `NatsDeployment` this wraps
    (`multistack.queue.drivers.nats.deployment` -- Helm/kubectl mechanics only, no
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
    expects everywhere below. Idempotent throughout: a second run adopts
    both existing Helm releases, and re-applying a stream or consumer
    that is already there is a no-op.
    """
    # Same self-heal as stage_objectstore(): if the storage stage didn't
    # run in this process, nothing published a StorageClass, so name the
    # one that is already on the cluster. The backend still verifies it.
    if "storage_class" not in stack:
        stack.provide(storage_class="longhorn")

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
            nack=NackOptions(
                # Same local-chart escape hatch, same flaky tarball host.
                # Namespace and nats_url are not set here on purpose --
                # the driver derives both from this spec, so the
                # controller cannot end up watching a NATS other than
                # the one above.
                chart=os.environ.get("MULTISTACK_NACK_CHART", "nack"),
            ),
        ),
    )
    backend = QueueBackend()
    for warning in backend.check_prerequisites(queue):
        print(f"[queue] warning: {warning}")
    # Printed before the call rather than after it. This is the slowest
    # stage in the file -- the servers' rollout, then the controller's,
    # then the Stream/Consumer CRDs becoming Established -- and
    # `NackInstaller` reports through `logging`, which nothing here
    # configures a handler for. Its account of what it did goes nowhere,
    # and silence this long reads as a hang.
    print(
        f"[stack] deploying the backbone into {queue.resolved_namespace} "
        "(servers, then NACK, then streams, then consumers)"
    )
    endpoint = backend.create(queue)
    stack.record(queue)  # publishes event_backbone_url
    print(f"[stack] queue at {endpoint}")

    # Consumers as well as streams. A stream with no consumer looks
    # fine from `list_streams` alone and is exactly the state that left
    # the limiters unready, so the thing that was invisible is the thing
    # worth printing.
    for name in backend.list_streams(queue):
        print(f"[stack]   stream {name}")
        for consumer in backend.list_consumers(queue, name):
            print(f"[stack]     consumer {consumer}")
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
    backbone = (
        {"event_backbone_url": event_backbone_url}
        if event_backbone_url
        else {"allow_no_backbone": True}
    )
    if not event_backbone_url:
        print(
            "[stack] no event backbone: enricher deploys enriching "
            "nothing (add queue to MULTISTACK_STAGES, or set "
            "MULTISTACK_EVENT_BACKBONE, to enforce)"
        )

    enricher = stack.build(
        Enricher,
        affinity=SERVICE_AFFINITY,
        options=EnricherOptions(
            tokenizer_url=stack.get("tokenizer_url", ""),
            image_repository=image_repo("enricher"),
            image_tag=image_tag(),
        ),
        node_selector=({"kubernetes.io/hostname": node_name(IMAGE_NODE)} if IMAGE_NODE else None),
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
    backbone = (
        {"event_backbone_url": event_backbone_url}
        if event_backbone_url
        else {"allow_no_backbone": True}
    )
    if not event_backbone_url:
        print(
            "[stack] no event backbone: policies deploy always-allow "
            "(add queue to MULTISTACK_STAGES, or set "
            "MULTISTACK_EVENT_BACKBONE, to enforce)"
        )

    # None when the cache stage did not run this time, which leaves each
    # chart's ConfigMap value standing -- correct for a Valkey with auth
    # disabled, and a NOAUTH at first request for one without. The guard
    # in main() says so rather than letting it be discovered.
    auth = {"cache_auth_url": cache_auth_url} if cache_auth_url else {}

    rpm = stack.build(
        Policy,
        type="rpm",
        affinity=SERVICE_AFFINITY,
        limits=RateLimits(user_default=60, model_default=600),
        options={"image_repository": image_repo("rate-limiter-rpm"), "image_tag": image_tag()},
        **auth,
        **backbone,
    )
    # No tokenizer wiring here: since ADR-030 the fallback lives in the
    # `enricher` service (TOKENIZER_URL), which this stack does not stage
    # (see multistack.enricher / examples/enricher/install.py).
    # tokenizer_url is still published above for whatever eventually
    # consumes it -- by hand, or by that example, until enricher gets a
    # stage here.
    tpm = stack.build(
        Policy,
        type="tpm",
        affinity=SERVICE_AFFINITY,
        limits=RateLimits(user_default=100_000, model_default=1_000_000),
        options={"image_repository": image_repo("rate-limiter-tpm"), "image_tag": image_tag()},
        **auth,
        **backbone,
    )

    for policy in (rpm, tpm):
        for warning in backend.check_prerequisites(policy):
            print(f"[{policy.type}] warning: {warning}")
        endpoints.append(backend.create(policy))
    stack.record(tpm)  # publishes policy_endpoint
    print(f"[stack] policies at {', '.join(endpoints)}")
    return endpoints


def stage_gateway(upstream: str, policy_endpoints: list, cache_auth_url: str | None = None) -> str:
    """The authenticated front door, in front of inference.

    `cache_auth_url` is the gateway's API-key cache. Without it the chart
    falls back to its own `redis://localhost:6379/0`, which nothing
    listens on: every request then re-verified its key against the org
    plane and logged `apikey.cache_get_degraded` -- working, but no cache,
    and found that way on the 2026-10-26 rebuild. It goes in the Secret
    rather than the chart's ConfigMap because it carries the password,
    and the Secret is read after the ConfigMap so it wins. Same Valkey,
    same database, as the org plane, which is what lets a key revoked
    there (`DEL apikey:{hash}`) stop working here at once.
    """
    key_secret = "gateway-keys"
    # generate=True: nobody needs to *choose* an API key, so Enter is
    # the expected answer. No owning capability module to call
    # store_secret() from -- Gateway.api_key_secret is a Secret *name*,
    # never the key itself (same shape Billing.existing_secret already
    # has for Stripe's secrets) -- so it is pushed into Vault here,
    # glue-level, the moment it's minted.
    api_key = prompt_secret(
        "Gateway API key",
        env_var="MULTISTACK_GATEWAY_API_KEY",
        generate=True,
    )
    # Kept, because store_secret() never raises: it returns False for a
    # Vault that was never deployed, is mid-rollout, or was trimmed out of
    # MULTISTACK_STAGES (see multistack/vault/client.py). Printing "stored
    # in Vault" without reading this said so even when nothing had been.
    api_key_in_vault = default_vault_client().store_secret(
        "gateway/api-key",
        {"value": api_key},
        deployment_name="modelgateway",
    )

    # Read off the spec, not typed out: the Secret has to land in the
    # namespace the release will look for it in, and that namespace is
    # the spec's own default, so naming it twice is how they drift apart.
    namespace = Gateway.DEFAULT_NAMESPACES["modelgateway"]
    _ensure_namespace(namespace)

    # Through stdin, never argv: `kubectl create secret --from-literal`
    # puts the key in the process arguments, readable from /proc.
    _apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": key_secret, "namespace": namespace},
            "type": "Opaque",
            "data": {
                "API_KEY": base64.b64encode(api_key.encode()).decode(),
                # Both keys together, in one apply. `kubectl apply` three-way
                # merges, so applying a subset of the keys later would delete
                # whichever this object already had and this manifest omits.
                "ORG_VERIFY_API_KEY": base64.b64encode(_org_verify_key().encode()).decode(),
                **(
                    {"VALKEY_URL": base64.b64encode(cache_auth_url.encode()).decode()}
                    if cache_auth_url
                    else {}
                ),
            },
        }
    )
    stack.provide(api_key_secret=key_secret)
    if not cache_auth_url:
        print("[gateway] warning: no cache this run, so API keys are re-verified on every request")

    # Derived from the organization plane's own spec rather than typed
    # out, the same way stage_portal derives its api_upstream. It has to
    # be the *organization* plane — only that one resolves an API key —
    # and a Stack records whichever plane went last without being able to
    # tell them apart, which is exactly why Gateway.FROM_STACK
    # deliberately does not wire this field and the spec requires it.
    org_cp = ControlPlane(
        type="organization",
        kubeconfig_path=KUBECONFIG,
        existing_secret="unused-for-the-url",
    )
    org_cp_url = os.environ.get("MULTISTACK_ORG_CP_URL", org_cp.endpoint)

    gateway = stack.build(
        Gateway,
        affinity=SERVICE_AFFINITY,
        options={"image_repository": image_repo("model-gateway"), "image_tag": image_tag()},
        upstream_url=upstream,
        org_cp_internal_url=org_cp_url,
        policy_endpoints=policy_endpoints,
        # closed: if the chain cannot be reached, reject. `open` prefers
        # availability over enforcement — the wrong default for anything
        # that bills.
        policy_fail_mode="closed",
        # GET /v1/models only lists model_routes' keys -- chat/completions
        # itself already falls back to upstream_url for any unmatched
        # model name (service.py's Service.__init__/_target), so this
        # isn't needed for inference to work, only for the org portal's
        # chat page to have anything to put in its model picker.
        #
        # So the key has to be a name the upstream will *accept*, not
        # just a label: the picker sends back whatever it was shown. It
        # was MODEL_KEY, which is right for vLLM and wrong for KServe --
        # KServe answers to its InferenceService's name -- so under
        # MULTISTACK_INFERENCE_TYPE=kserve the one thing this value
        # exists for was the one thing it broke, with
        # `404 Model with name Qwen2.5-0.5B-Instruct does not exist`.
        # `served_name` knows the difference; see its docstring.
        model_routes={_served_model_name(): upstream},
    )
    backend = GatewayBackend()
    for warning in backend.check_prerequisites(gateway):
        print(f"[gateway] warning: {warning}")
    endpoint = backend.create(gateway)
    stack.record(gateway)  # publishes gateway_endpoint
    if api_key_in_vault:
        print(f"[stack] gateway at {endpoint} (API key stored in Vault at secret/gateway/api-key)")
    else:
        # Not fatal -- the key is in the gateway-keys Secret applied above
        # either way, so it stays recoverable with kubectl. But that is a
        # worse answer than printing it here, which is what this line did
        # before Vault existed, and silence would leave someone hunting
        # for a Vault path that was never written.
        print(f"[stack] gateway at {endpoint}  (API key: {api_key})")
        print(
            f"[stack] no Vault stored it — recover it later from the "
            f"{key_secret} Secret in {namespace}"
        )
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
        print(
            "[stack] skipped: billing needs its own database, which "
            "only the database stage produces (billing is a row in "
            "this file's own DATABASES)"
        )
        return None

    namespace = Billing.DEFAULT_NAMESPACES["stripe"]
    _ensure_namespace(namespace)

    data = {
        "DATABASE_URL_BILLING": db_urls["billing"],
        "SERVICE_API_KEY": _admin_service_api_key(),
    }
    stripe_key = os.environ.get("MULTISTACK_STRIPE_SECRET_KEY", "")
    stripe_webhook = os.environ.get("MULTISTACK_STRIPE_WEBHOOK_SECRET", "")
    if stripe_key:
        data["STRIPE_SECRET_KEY"] = stripe_key
    if stripe_webhook:
        data["STRIPE_WEBHOOK_SECRET"] = stripe_webhook
    if not (stripe_key and stripe_webhook):
        print(
            "[stack] no Stripe credentials: billing meters usage but "
            "reports nothing to Stripe (MULTISTACK_STRIPE_SECRET_KEY / "
            "MULTISTACK_STRIPE_WEBHOOK_SECRET)"
        )
    else:
        # No owning capability module either: Billing.existing_secret is
        # a Secret *name*, never a field for the Stripe values themselves
        # (see multistack/billing/spec.py's own module docstring), so
        # there is no backend create() to push this from -- here, right
        # where the two env vars are read, is as close to "generated" as
        # a pair of operator-supplied credentials gets.
        default_vault_client().store_secret(
            "billing/stripe",
            {"secret_key": stripe_key, "webhook_secret": stripe_webhook},
            # Billing's own tracked name (Billing.type) -- unlike the two
            # shared service keys above, this one has exactly one owner.
            deployment_name="stripe",
        )

    # Through stdin, never argv, same as every other Secret in this file.
    _apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": BILLING_SECRET, "namespace": namespace},
            "type": "Opaque",
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
        }
    )

    backbone = (
        {"event_backbone_url": event_backbone_url}
        if event_backbone_url
        else {"allow_no_backbone": True}
    )
    if not event_backbone_url:
        print(
            "[stack] no event backbone: billing deploys metering "
            "nothing (add queue to MULTISTACK_STAGES, or set "
            "MULTISTACK_EVENT_BACKBONE, to enforce)"
        )

    billing = stack.build(
        Billing,
        affinity=SERVICE_AFFINITY,
        options={"image_repository": image_repo("billing"), "image_tag": image_tag()},
        existing_secret=BILLING_SECRET,
        node_selector=({"kubernetes.io/hostname": node_name(IMAGE_NODE)} if IMAGE_NODE else None),
        **backbone,
    )
    backend = BillingBackend()
    for warning in backend.check_prerequisites(billing):
        print(f"[billing] warning: {warning}")
    endpoint = backend.create(billing)
    stack.record(billing)  # publishes billing_endpoint
    print(f"[stack] billing at {endpoint}")
    return endpoint


def stage_controlplane(db_urls: dict, cache_auth_url: str) -> None:
    """Both control planes, admin and organization.

    The one place credentials are assembled. The charts need an
    *authenticated* DATABASE_URL_ADMIN / DATABASE_URL_ORG, and the `database_url` the stack
    publishes is deliberately credential-free — so the two cannot be
    connected, and the authenticated URL goes into a Secret here.

    Taken per plane from `db_urls` rather than read back off the stack:
    each plane has its own database, and a Stack holding two of one
    capability publishes only whichever was recorded last.
    """
    # Authenticated, and taken from the argument rather than read back
    # off the stack for the same reason the database URL is: what the Stack
    # publishes has no password in it, and these charts need one.
    cache = cache_auth_url
    # One JWT secret, shared: each plane verifies tokens the other issued,
    # so a mismatch makes every cross-service call fail authentication.
    # No owning capability module either, same as the two shared service
    # keys above -- pushed into Vault here, the moment it's minted,
    # rather than from either plane's own backend.
    jwt = prompt_secret(
        "JWT signing secret (shared by both control planes)",
        env_var="MULTISTACK_JWT_SECRET",
        generate=True,
    )
    # Recorded against the admin plane's own tracked name, the same
    # convention _admin_service_api_key's own storing already follows for
    # a value genuinely shared by both planes with no single owner.
    default_vault_client().store_secret(
        "shared/jwt-secret",
        {"value": jwt},
        deployment_name="admin",
    )

    backend = ControlPlaneBackend()
    for cp_type in ("admin", "organization"):
        name = f"{cp_type}-cp-secrets"
        data = {
            {"admin": "DATABASE_URL_ADMIN", "organization": "DATABASE_URL_ORG"}[cp_type]: db_urls[
                cp_type
            ],
            "VALKEY_URL": cache,
            "JWT_SECRET": jwt,
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
            admin_api_key = secrets.token_urlsafe(24)
            # Admin-portal's own login credential: no owning capability
            # module (ControlPlane.existing_secret is a Secret name, not
            # a field for the key itself), so pushed into Vault here,
            # against this plane's own tracked name, the moment it's
            # minted.
            default_vault_client().store_secret(
                "controlplane/admin-api-key",
                {"value": admin_api_key},
                deployment_name="admin",
            )
            data["ADMIN_API_KEY"] = admin_api_key
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
        _apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace},
                "type": "Opaque",
                "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
            }
        )

        control_plane = stack.build(
            ControlPlane,
            type=cp_type,
            affinity=SERVICE_AFFINITY,
            options={
                "image_repository": image_repo(f"{cp_type}-control-plane"),
                "image_tag": image_tag(),
            },
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
        stack.record(control_plane)  # publishes controlplane_endpoint
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
            type=portal_type,
            kubeconfig_path=KUBECONFIG,
            existing_secret="unused-for-the-url",
        )
        # Only the organization portal has a chat page to proxy through —
        # Portal.gateway_upstream's own docstring calls this optional for
        # exactly that reason. Left unset, the chart renders no `/mg`
        # location at all: chat's calls to it fall through to the SPA's
        # own index.html (200, wrong content-type), which is the exact
        # silent-failure shape api_upstream's docstring already warns
        # about for a missing api_prefix. Gateway.endpoint is a pure
        # function of name/namespace, so this is safe to compute even
        # when `gateway` is not in this run's STAGES.
        gateway_upstream = (
            Gateway(
                kubeconfig_path=KUBECONFIG,
                upstream_url="unused-for-the-url",
                api_key_secret="unused-for-the-url",
                org_cp_internal_url="unused-for-the-url",
            ).endpoint
            if portal_type == "organization"
            else ""
        )
        # -- glue -- the only first-party images not already threaded
        # through `image_repo` above. PortalOptions spells both
        # repositories out, and has to: `ui/chart` can only default to
        # one of the two, and defaults to the organization portal, so an
        # admin portal left to the chart would deploy the wrong image.
        # Spelled out is right; spelled out *in the spec* is not, because
        # it means these two alone ignore MULTISTACK_IMAGE_REGISTRY and a
        # fork keeps pulling this repo's portals. Passed here instead,
        # with the spec's own values left as the fallback for a deploy
        # outside a git checkout -- the same thing `image_repo` returning
        # None means for every other service.
        portal_options: dict = {}
        portal_repo = image_repo(f"{portal_type}-portal")
        if portal_repo:
            portal_options["image_repository"] = portal_repo
        if image_tag():
            portal_options["image_tag"] = image_tag()

        portal = stack.build(
            Portal,
            type=portal_type,
            api_upstream=api.endpoint,
            gateway_upstream=gateway_upstream,
            node_selector=(
                {"kubernetes.io/hostname": node_name(IMAGE_NODE)} if IMAGE_NODE else None
            ),
            **({"options": portal_options} if portal_options else {}),
        )
        for warning in backend.check_prerequisites(portal):
            print(f"[{portal_type}-portal] warning: {warning}")
        endpoint = backend.create(portal)
        stack.record(portal)  # publishes portal_endpoint
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
        print(
            "[stack] skipped: nothing to route through — the ingress "
            "stage did not run, so there is no front door yet"
        )
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
        (
            "model-gateway",
            Gateway.DEFAULT_NAMESPACES["modelgateway"],
            f"{ModelGatewayOptions().release_name}-model-gateway",
            8080,
            API_HOST,
            "/v1",
        ),
        (
            "admin-portal",
            Portal.DEFAULT_NAMESPACES["admin"],
            Portal.OPTIONS_FOR_TYPE["admin"]().release_name,
            80,
            ADMIN_HOST,
            "/",
        ),
        (
            "organization-portal",
            Portal.DEFAULT_NAMESPACES["organization"],
            Portal.OPTIONS_FOR_TYPE["organization"]().release_name,
            80,
            ORG_HOST,
            "/",
        ),
    ]

    # The observability consoles, added 2026-10-12. kube-prometheus-stack
    # installs three web UIs and routes none of them, so reaching Grafana
    # meant a port-forward every time -- and Prometheus and Alertmanager,
    # which is where you look when Grafana says something is wrong, were
    # no easier.
    #
    # Service names are the chart's, which keys everything off the release
    # name (`Observability.options.release_name`) rather than being typed
    # here, for the same reason the three above are derived: a wrong
    # backend does not error, it 404s at the door.
    #
    # NOTE ON ACCESS: Grafana has a login. Prometheus and Alertmanager
    # have none at all -- anyone who can reach the front door can read
    # every metric in the cluster, and can silence any alert. That is
    # usually fine on a lab LAN and is not fine anywhere else; put them
    # behind the gateway's auth, a NetworkPolicy, or drop them from this
    # list before this stack meets a network you do not control.
    observability = Observability(kubeconfig_path=KUBECONFIG)
    monitoring_ns = observability.resolved_namespace
    release = observability.options.release_name
    targets += [
        ("grafana", monitoring_ns, f"{release}-grafana", 80, GRAFANA_HOST, "/"),
        ("prometheus", monitoring_ns, f"{release}-prometheus", 9090, PROMETHEUS_HOST, "/"),
        ("alertmanager", monitoring_ns, f"{release}-alertmanager", 9093, ALERTMANAGER_HOST, "/"),
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
        print(f"[stack] {hostname}{prefix if prefix != '/' else ''} -> {service}:{port}")

    # One recorded, not three: Route PROVIDES `route_url` and a Stack
    # holding three of one capability publishes whichever went last. The
    # model gateway is the one a later layer would mean, so it is named
    # rather than left to loop order.
    stack.record(routes["model-gateway"])  # publishes route_url
    print(
        f"[stack] routed {len(urls)} services through the front door; "
        f"they need DNS for {API_HOST}, {ADMIN_HOST} and {ORG_HOST} "
        "pointing at it"
    )
    return urls[0]


def stage_observability() -> str:
    """Prometheus, Alertmanager and Grafana.

    Last because it needs the StorageClass, and because everything above
    is worth watching.
    """
    # Same self-heal as stage_objectstore()/stage_queue(): if the storage
    # stage didn't run in this process, nothing published a StorageClass,
    # so name the one that is already on the cluster. The backend still
    # verifies it.
    if "storage_class" not in stack:
        stack.provide(storage_class="longhorn")

    observability = stack.build(
        Observability,
        # Same local-chart escape hatch as storage's LonghornOptions.chart
        # -- prometheus-community's index.yaml is on GitHub Pages, but the
        # tarball comes from release-assets.githubusercontent.com, the
        # same flaky leg that hit Longhorn/cnpg/istio/nats.
        options=KubePrometheusStackOptions(
            chart=os.environ.get("MULTISTACK_KUBE_PROMETHEUS_STACK_CHART", "kube-prometheus-stack"),
        ),
    )
    endpoint = ObservabilityBackend().create(observability)
    stack.record(observability)
    print(f"[stack] grafana at {endpoint}")
    return endpoint


def stage_vllm_metrics() -> None:
    """Wire vLLM's built-in `/metrics` into kube-prometheus-stack's Prometheus.

    Last, and deliberately separate from `inference`: it needs the
    `ServiceMonitor` CRD the `observability` stage's chart installs —
    applying one earlier fails with "no matches for kind ServiceMonitor"
    on a cluster that hasn't run that stage yet.

    A re-`create()` against the already-deployed Inference service (the
    standalone version of this is examples/observability/vllm_metrics.py).

    NOT a no-op Deployment-wise, despite this stage's original docstring
    claiming otherwise: InferenceBackend().create() does a full manifest
    apply, which 3-way-merges against the *previous* applied config. A
    spec built with only name/namespace/service_monitor_labels set (every
    other field at its class default) got applied on 2026-09-25 and wiped
    node_selector + tolerations (pod went permanently Pending: 0/4 nodes
    schedulable, untolerated `dedicated=inference` taint) *and* silently
    swapped the model for the un-mirrored HuggingFace one at 4096 context
    with no dtype pin -- the old pod happened to survive only because the
    broken replacement never got past Pending. Fixed by sharing every
    field stage_inference() sets through _vllm_qwen_kwargs(), so the two
    can no longer drift apart and the re-apply is actually a no-op.
    """
    if "inference" not in STAGES:
        print(
            f"[stack] vllm_metrics assumes the {INFERENCE_TYPE} deployment "
            "is already there (inference not in this run's STAGES)"
        )

    # Only used to read `options.release_name` -- kube-prometheus-stack's
    # Prometheus only scrapes ServiceMonitors carrying this label by
    # default (its own `serviceMonitorSelectorNilUsesHelmValues`
    # convention).
    release_name = Observability(kubeconfig_path=KUBECONFIG).options.release_name

    # Not stack.build(): this stage is meant to also run standalone
    # (against a vllm-qwen deployed by an earlier, separate process), so
    # there is no Stack with s3_endpoint_url/s3_secret_name published to
    # read FROM_STACK. SeaweedFSCluster.endpoint() is a pure function of
    # name/namespace, so it's safe to recompute rather than look up.
    s3_endpoint_url = SeaweedFSCluster(
        kubeconfig_path=KUBECONFIG,
        name="seaweedfs",
        namespace="seaweedfs",
    ).endpoint()

    # Whichever implementation this stack runs, not vLLM unconditionally.
    # `InferenceBackend().create()` is a full manifest apply, so calling
    # it with a vLLM spec on a KServe cluster does not attach a
    # ServiceMonitor to anything -- it deploys a vLLM. That is what
    # happened on 2026-10-09: `vllm-qwen` appeared beside a working
    # `kserve-qwen-predictor`, two servers for one model, each asking for
    # 4 CPU and 12Gi. Both drivers render a ServiceMonitor from
    # `service_monitor_labels`, so the only thing this stage needed was
    # to build the same spec its own inference stage builds.
    if INFERENCE_TYPE == "kserve_llm":
        # The spec refuses service_monitor_labels for kserve_llm: the
        # labels KServe puts on the LLMInferenceService workload Service
        # have not been confirmed on a live cluster, and a ServiceMonitor
        # that matches nothing fails silently. Skipped, not guessed.
        print(
            "[stack] skipped: ServiceMonitor for kserve_llm is not supported "
            "yet; scrape llm-qwen-kserve-workload-svc with one of your own"
        )
        return
    if INFERENCE_TYPE == "kserve":
        kwargs = _kserve_qwen_kwargs(s3_endpoint_url=s3_endpoint_url)
    else:
        # "float32", not None: dtype=None auto-detects from the target
        # node's CPU flags at create() time via `nodes=`, which this call
        # site has no way to pass (no Node to detect from, standalone) --
        # pinned to the value stage_inference() already resolved there.
        kwargs = _vllm_qwen_kwargs(s3_endpoint_url=s3_endpoint_url, dtype="float32")

    service = Inference(
        kubeconfig_path=KUBECONFIG,
        **kwargs,
        service_monitor_labels={"release": release_name},
    )
    InferenceBackend().create(service)
    print(
        f"[stack] {INFERENCE_TYPE} metrics scraped by {release_name} "
        f"(ServiceMonitor {service.name}-metrics)"
    )


def main() -> int:
    _ensure_cp_tools()
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

    vault = None
    if "vault" in STAGES:
        _banner("vault")
        vault = stage_vault()

    tenant = None
    if "objectstore" in STAGES:
        _banner("objectstore")
        tenant = stage_objectstore()
    if tenant is None:
        # Later stages re-run alone: rebuild the spec, and create() adopts
        # the live cluster's existing credentials rather than rotating them.
        # Built directly rather than through the stack — nothing recorded
        # a StorageClass this run, and the cluster already has one.
        tenant = SeaweedFSCluster(
            kubeconfig_path=KUBECONFIG,
            name="seaweedfs",
            namespace="seaweedfs",
            master_replicas=1,
            filer_replicas=1,
            volume_replicas=2,
            volume_size="20Gi",
            replication="001",
            storage_class="longhorn",
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
        if {"policy", "gateway", "controlplane"} & set(STAGES):
            live = Cache(kubeconfig_path=KUBECONFIG, name="valkey", namespace=CACHE_NS)
            stack.record(live)  # publishes cache_url, credential-free
            cache_auth_url = _cache_authenticated(live.endpoint, _cache_password())
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
    elif not event_backbone_url and ({"policy", "gateway", "enricher", "billing"} & set(STAGES)):
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
            "MULTISTACK_INFERENCE_ENDPOINT", _inference_fallback_endpoint()
        )
        gateway_endpoint = stage_gateway(upstream, policy_endpoints, cache_auth_url)

    billing_endpoint = None
    if "billing" in STAGES:
        _banner("billing")
        if db_urls is None:
            print(
                "[stack] skipped: billing needs its own authenticated "
                "DATABASE_URL_BILLING, which only the database stage produces. "
                "Re-run with database in MULTISTACK_STAGES, with "
                "MULTISTACK_BILLING_DB_PASSWORD set to the existing "
                "password."
            )
        else:
            billing_endpoint = stage_billing(db_urls, event_backbone_url)

    if "controlplane" in STAGES:
        _banner("controlplane")
        if db_urls is None:
            print(
                "[stack] skipped: the control plane Secrets need each "
                "plane's own authenticated DATABASE_URL_ADMIN / DATABASE_URL_ORG, which only the "
                "database stage produces. Re-run with database in "
                "MULTISTACK_STAGES, with MULTISTACK_ADMIN_DB_PASSWORD "
                "and MULTISTACK_ORGANIZATION_DB_PASSWORD set to the "
                "existing passwords."
            )
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

    if "vllm_metrics" in STAGES:
        _banner("vllm_metrics")
        stage_vllm_metrics()

    print("\n" + "=" * 60)
    print("Stack ready.")
    print(f"  cluster     kubectl --kubeconfig {KUBECONFIG} get nodes")
    print(f"  storage     kubectl --kubeconfig {KUBECONFIG} get sc")
    if vault:
        print(f"  vault       {vault.ui_url}")
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
        print(
            f"""
Try:
  curl {endpoint}/v1/chat/completions \\
    -H 'Content-Type: application/json' \\
    -d '{{"model":"{_served_model_name()}","messages":[{{"role":"user","content":"hi"}}],"max_tokens":32}}'"""
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
