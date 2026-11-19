"""A new script-generation evaluation set from a seed.

    from fresh_cases import generate
    cases = generate(20261013)        # the same seed always gives the same cases

script_cases.py is the set the agent was developed against. This one is for
measuring it on requests it has never been run on: every seed gives another
set, so no set has to be used twice.

A case is assembled from parts drawn by the seed: what to build and on which
cluster, one or two settings said the way a user would say them, and the
names, addresses and secrets a user would give. The expected plan is
assembled from the same parts, so the two always agree, and
tests/test_fresh_cases.py holds every generated case to the checks the
hand-written set passes: the plan validates against the real tools with its
dependency chain, the request states every value only the user can give, an
ask case is rejected for the value its reply must ask for, and a pause case
stops on the options it lists.

Each kind of outcome is covered:

  build   everything needed is in the request
  ask     a value only the user can give is left out, or the request asks
          for something the SDK does not accept
  pause   a decision is left open (new cluster or existing, requests or
          tokens) and the conversation continues with the user's answers

The reference passage of a build case is where the SDK defines the field
its setting uses, found with the RAG service's own chunker.
"""

from __future__ import annotations

import copy
import inspect
import random
import sys
import typing
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from script_cases import EXISTING_CLUSTER, FULL_STACK, NATS_DEFAULT, NEW_CLUSTER, WIRED, ScriptCase

REPO_ROOT = Path(__file__).resolve().parents[4]

# The set evaluated on 2026-10-13.
SEED = 20261013


@dataclass(frozen=True)
class Setting:
    """One thing a request asks of a component: the sentence a user would
    write, the fields it sets, the lines the script then carries, and the
    SDK fields that define them (`Class.field`)."""

    component: str
    sentence: str
    patch: dict
    lines: tuple[str, ...]
    fields: tuple[str, ...]


def _s(component: str, sentence: str, patch: dict, lines, *fields: str) -> Setting:
    return Setting(component, sentence, patch, (lines,) if isinstance(lines, str) else tuple(lines), fields)


# ---- what a user knows about their own platform


@dataclass(frozen=True)
class Facts:
    kubeconfig: str
    storage_class: str
    model_url: str
    gateway_secret: str
    cp_secret: str
    db: str
    database: str
    owner: str
    password: str
    cache: str


_KUBECONFIGS = (
    "/etc/rancher/rke2/rke2.yaml",
    "/home/ops/.kube/config",
    "/root/.kube/config",
    "/opt/mcw/kubeconfig.yaml",
    "/home/deploy/clusters/prod.kubeconfig",
)
_STORAGE_CLASSES = ("longhorn", "local-path", "ceph-rbd", "nfs-client", "fast-ssd")
_MODEL_URLS = (
    "http://vllm.ai.svc:8000/v1",
    "http://vllm.serving.svc.cluster.local:8000/v1",
    "http://10.42.7.20:8000/v1",
    "http://llm.models.svc:8080/v1",
)
_GATEWAY_SECRETS = ("gateway-keys", "gw-api-keys", "model-gateway-keys", "edge-api-keys")
_ORG_SECRETS = ("org-cp-secrets", "orgcp-env", "controlplane-org-secrets", "org-control-plane")
_ADMIN_SECRETS = ("admin-cp-secrets", "admincp-env", "controlplane-admin-secrets")
_DB_CLUSTERS = ("platform-db", "core-pg", "tenant-db", "mcw-postgres", "accounts-pg")
_DATABASES = ("appdb", "platform", "tenants", "orgdata", "ledger")
_OWNERS = ("appuser", "platform_app", "svc_app", "dbadmin", "accounts_rw")
_CACHES = ("platform-cache", "session-store", "rl-cache", "edge-valkey", "shared-cache")
_MODELS = (
    "Qwen/Qwen2.5-1.5B-Instruct",
    "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
    "microsoft/Phi-3-mini-4k-instruct",
    "HuggingFaceTB/SmolLM2-1.7B-Instruct",
)
_ROUTED_MODELS = ("llama-3.1-8b", "qwen2.5-7b", "mistral-7b-instruct", "phi-3-mini", "gemma-2-9b")
_LABELS = (("pool", "edge"), ("workload", "platform"), ("tier", "frontend"), ("zone", "rack-b"))
_SSH_USERS = ("ubuntu", "rocky", "ops", "deploy")
_SSH_KEYS = ("~/.ssh/id_ed25519", "~/.ssh/cluster_rsa", "/home/ops/.ssh/mcw-nodes", "~/.ssh/lab-key")
_CLUSTER_NAMES = ("lab", "edge", "staging", "west-2", "obs", "pilot")


def _password(rng: random.Random, word: str) -> str:
    upper, lower = "ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghjkmnpqrstuvwxyz"
    return f"{rng.choice(upper)}{rng.choice(lower)}{rng.randrange(2, 10)}-{word}-{rng.randrange(1000, 10000)}"


def _facts(rng: random.Random) -> Facts:
    return Facts(
        kubeconfig=rng.choice(_KUBECONFIGS),
        storage_class=rng.choice(_STORAGE_CLASSES),
        model_url=rng.choice(_MODEL_URLS),
        gateway_secret=rng.choice(_GATEWAY_SECRETS),
        cp_secret=rng.choice(_ORG_SECRETS),
        db=rng.choice(_DB_CLUSTERS),
        database=rng.choice(_DATABASES),
        owner=rng.choice(_OWNERS),
        # Not a word any component answers to: a password is not a request.
        password=_password(rng, rng.choice(("plat", "main", "acct", "base"))),
        cache=rng.choice(_CACHES),
    )


def _kubeconfig_says(rng: random.Random, path: str) -> str:
    return rng.choice(
        (
            f"The cluster's kubeconfig is {path}.",
            f"Deploy onto the cluster whose kubeconfig is at {path}.",
            f"Use our existing cluster; its kubeconfig file is {path}.",
        )
    )


def _storage_says(rng: random.Random, name: str) -> str:
    return rng.choice(
        (
            f"The cluster's StorageClass is {name}.",
            f"Use the StorageClass {name} that the cluster already has.",
            f"Persistent volumes come from the cluster's existing StorageClass, {name}.",
        )
    )


def _model_says(rng: random.Random, url: str) -> str:
    return rng.choice(
        (
            f"Our models are served by vLLM at {url}.",
            f"We already run vLLM at {url}, so point the gateway at that.",
            f"The model server is a vLLM instance at {url}.",
        )
    )


def _gateway_secret_says(rng: random.Random, name: str) -> str:
    return rng.choice(
        (
            f"Gateway API keys are in the Secret {name}.",
            f"The gateway's API keys live in the Kubernetes Secret {name}.",
            f"Use the Secret {name} for the gateway's API keys.",
        )
    )


def _cp_secret_says(rng: random.Random, kind: str, name: str) -> str:
    return rng.choice(
        (
            f"The {kind} control plane's secrets are in the Secret {name}.",
            f"The {kind} control plane reads its secrets from the Secret {name}.",
            f"For the {kind} control plane, the existing Secret is {name}.",
        )
    )


def _postgres_says(rng: random.Random, f: Facts, *, password: bool = True) -> str:
    secret = f", password {f.password}" if password else ""
    return rng.choice(
        (
            f"Call the Postgres cluster {f.db}, with database {f.database} owned by {f.owner}{secret}.",
            f"Name the PostgreSQL cluster {f.db}; it needs a database {f.database} whose owner is "
            f"{f.owner}{secret}.",
            f"The Postgres cluster should be called {f.db} and hold the database {f.database} "
            f"(owner {f.owner}{secret}).",
        )
    )


def _cache_says(rng: random.Random, name: str) -> str:
    return rng.choice(
        (
            f"Call the cache {name}.",
            f"Name the Valkey cache {name}.",
            f"The cache should be called {name}.",
        )
    )


def _request_path_says(rng: random.Random, f: Facts, *, leave_out: str = "", storage: bool = False) -> list[str]:
    """The facts a request-path request gives, in a drawn order. `leave_out`
    names the one an ask or pause case withholds."""
    sentences = {
        "kubeconfig": _kubeconfig_says(rng, f.kubeconfig),
        "model": _model_says(rng, f.model_url),
        "gateway_secret": _gateway_secret_says(rng, f.gateway_secret),
        "cp_secret": _cp_secret_says(rng, "organization", f.cp_secret),
        "postgres": _postgres_says(rng, f, password=leave_out != "password"),
        "cache": _cache_says(rng, f.cache),
    }
    if storage:
        sentences["storage"] = _storage_says(rng, f.storage_class)
    kept = [text for key, text in sentences.items() if key != leave_out]
    rng.shuffle(kept)
    return kept


def _request_path(f: Facts) -> dict:
    return {
        "kubeconfig_path": f.kubeconfig,
        "cnpg": {
            "kubeconfig_path": WIRED,
            "name": f.db,
            "database": {"name": f.database, "owner": f.owner, "password": f.password},
        },
        "valkey": {"kubeconfig_path": WIRED, "name": f.cache},
        "controlplane": {"kubeconfig_path": WIRED, "type": "organization", "existing_secret": f.cp_secret},
        "gateway": {
            "kubeconfig_path": WIRED,
            "upstream_url": f.model_url,
            "api_key_secret": f.gateway_secret,
            "org_cp_internal_url": WIRED,
        },
    }


def _with_queue(args: dict, f: Facts) -> dict:
    """`args` with the event queue and the gateway publishing to it."""
    return {
        **args,
        "existing_storage_class": f.storage_class,
        "queue": {"kubeconfig_path": WIRED, "storage_class": WIRED},
        "gateway": {**args["gateway"], "event_backbone_url": NATS_DEFAULT},
    }


def _event_path(f: Facts) -> dict:
    return {
        **_with_queue(_request_path(f), f),
        "tokenizer": {"kubeconfig_path": WIRED},
        "enricher": {"kubeconfig_path": WIRED, "event_backbone_url": NATS_DEFAULT},
    }


_REQUEST_PATH = ("valkey", "cnpg", "controlplane", "gateway")
_EVENT_PATH = (*_REQUEST_PATH, "queue", "tokenizer", "enricher")


# ---- what a request can ask of each component


def _gateway(rng: random.Random) -> list[Setting]:
    replicas, port = rng.choice((3, 4, 6)), rng.choice((8081, 8443, 9000))
    seconds, inflight = rng.choice((90, 150, 300)), rng.choice((150, 400, 2500))
    tag = rng.choice(("v1.8.2", "2026.09.3", "v2.0.0-rc1"))
    key, value = rng.choice(_LABELS)
    first, second = rng.sample(_ROUTED_MODELS, 2)
    routes = {
        first: f"http://vllm-{first.split('-')[0]}.ai.svc:8000/v1",
        second: f"http://vllm-{second.split('-')[0]}.ai.svc:8000/v1",
    }
    return [
        _s("gateway", f"Run {replicas} replicas of the gateway.", {"replicas": replicas},
           f"replicas={replicas}", "Gateway.replicas"),
        _s("gateway", f"The gateway should listen on port {port}.", {"service_port": port},
           f"service_port={port}", "Gateway.service_port"),
        _s("gateway", f"Give a model up to {seconds} seconds to answer before the gateway times the request out.",
           {"request_timeout": seconds}, f"request_timeout={float(seconds)}", "Gateway.request_timeout"),
        _s("gateway", f"Let the gateway handle at most {inflight} requests at the same time.",
           {"options": {"max_inflight": inflight}}, f"max_inflight={inflight}", "ModelGatewayOptions.max_inflight"),
        _s("gateway", f"Deploy the gateway from image tag {tag}.", {"options": {"image_tag": tag}},
           f"image_tag={tag!r}", "ModelGatewayOptions.image_tag"),
        _s("gateway", f"Schedule the gateway only on nodes labelled {key}={value}.",
           {"node_selector": {key: value}}, f"node_selector={{{key!r}: {value!r}}}", "Gateway.node_selector"),
        _s("gateway", "Have the gateway include the full prompt and response text in the events it emits.",
           {"options": {"capture_bodies": True}}, "capture_bodies=True", "ModelGatewayOptions.capture_bodies"),
        _s("gateway",
           f"Requests for {first} go to {routes[first]} and requests for {second} to {routes[second]}; "
           "every other model goes to the default server.",
           {"model_routes": routes}, [f"{m!r}: {u!r}" for m, u in routes.items()], "Gateway.model_routes"),
    ]


def _controlplane(rng: random.Random) -> list[Setting]:
    replicas, port = rng.choice((3, 4, 5)), rng.choice((8100, 9001, 8088))
    return [
        _s("controlplane", f"Run {replicas} replicas of the organization control plane.", {"replicas": replicas},
           f"replicas={replicas}", "ControlPlane.replicas"),
        _s("controlplane", f"The organization control plane should listen on port {port}.", {"service_port": port},
           f"service_port={port}", "ControlPlane.service_port"),
        _s("controlplane", "Set the organization control plane's log level to DEBUG.", {"log_level": "DEBUG"},
           "log_level='DEBUG'", "ControlPlane.log_level"),
    ]


def _postgres(rng: random.Random) -> list[Setting]:
    instances, size = rng.choice((1, 2, 5)), rng.choice(("20Gi", "80Gi", "250Gi"))
    image = f"ghcr.io/cloudnative-pg/postgresql:{rng.choice(('16.6', '15.10', '17.2'))}"
    return [
        _s("cnpg", f"Run the Postgres cluster with {instances} instance{'s' if instances > 1 else ''}.",
           {"instances": instances}, f"instances={instances}", "Database.instances"),
        _s("cnpg", f"Give the Postgres cluster {size} of storage.", {"storage_size": size},
           f"storage_size={size!r}", "Database.storage_size"),
        _s("cnpg", f"Run Postgres from the image {image}.", {"image": image}, f"image={image!r}", "Database.image"),
    ]


def _cache(rng: random.Random) -> list[Setting]:
    version = rng.choice(("2.2.3", "2.5.0", "3.0.1"))
    return [
        _s("valkey", f"Install the cache from version {version} of the Valkey Helm chart.",
           {"options": {"chart_version": version}}, f"chart_version={version!r}", "ValkeyOptions.chart_version"),
    ]


def _queue(rng: random.Random) -> list[Setting]:
    servers, volume = rng.choice((5, 7)), rng.choice(("40Gi", "100Gi", "250Gi"))
    memory, tag = rng.choice(("1Gi", "4Gi", "8Gi")), rng.choice(("2.12.4-alpine", "2.11.9-alpine"))
    return [
        _s("queue", f"Run {servers} NATS servers.", {"options": {"replicas": servers}},
           f"replicas={servers}", "NatsQueueOptions.replicas"),
        _s("queue", f"Give each NATS server a {volume} volume for JetStream.", {"options": {"pvc_size": volume}},
           f"pvc_size={volume!r}", "NatsQueueOptions.pvc_size"),
        _s("queue", f"Cap JetStream's in-memory storage at {memory}.", {"options": {"memory_store_max_size": memory}},
           f"memory_store_max_size={memory!r}", "NatsQueueOptions.memory_store_max_size"),
        _s("queue", f"Use the NATS image tag {tag}.", {"options": {"image_tag": tag}},
           f"image_tag={tag!r}", "NatsQueueOptions.image_tag"),
    ]


def _tokenizer(rng: random.Random) -> list[Setting]:
    replicas, port = rng.choice((3, 5, 6)), rng.choice((8088, 9100, 8500))
    tag = rng.choice(("v0.9.1", "2026.08.2"))
    return [
        _s("tokenizer", f"Run {replicas} replicas of the tokenizer.", {"replicas": replicas},
           f"replicas={replicas}", "Tokenizer.replicas"),
        _s("tokenizer", f"The tokenizer should listen on port {port}.", {"service_port": port},
           f"service_port={port}", "Tokenizer.service_port"),
        _s("tokenizer", f"Deploy the tokenizer from image tag {tag}.", {"options": {"image_tag": tag}},
           f"image_tag={tag!r}", "TiktokenOptions.image_tag"),
    ]


def _enricher(rng: random.Random) -> list[Setting]:
    tries, wait = rng.choice((8, 12, 20)), rng.choice((45, 60, 90))
    millis = rng.choice((800, 1500, 3000))
    return [
        _s("enricher", f"The enricher should attempt an event no more than {tries} times.",
           {"options": {"max_deliver": tries}}, f"max_deliver={tries}", "EnricherOptions.max_deliver"),
        _s("enricher", f"Give the enricher {wait} seconds to acknowledge an event before it is redelivered.",
           {"options": {"ack_wait_seconds": wait}}, f"ack_wait_seconds={wait}", "EnricherOptions.ack_wait_seconds"),
        _s("enricher", f"The enricher should wait at most {millis} milliseconds for the tokenizer.",
           {"options": {"tokenizer_timeout_ms": millis}}, f"tokenizer_timeout_ms={millis}",
           "EnricherOptions.tokenizer_timeout_ms"),
    ]


def _billing(rng: random.Random) -> list[Setting]:
    meter = rng.choice(("llm_usage", "ai_tokens", "gateway_tokens"))
    batch, every, replicas = rng.choice((100, 200)), rng.choice((10, 30, 60)), rng.choice((2, 3))
    return [
        _s("billing", f"Report usage to Stripe under the meter event name {meter}.",
           {"options": {"stripe_meter_event_name": meter}}, f"stripe_meter_event_name={meter!r}",
           "StripeOptions.stripe_meter_event_name"),
        _s("billing", f"Have billing send usage to Stripe in batches of {batch}.",
           {"options": {"reporter_batch_size": batch}}, f"reporter_batch_size={batch}",
           "StripeOptions.reporter_batch_size"),
        _s("billing", f"The billing reporter should look for new usage every {every} seconds.",
           {"options": {"reporter_poll_interval_seconds": every}}, f"reporter_poll_interval_seconds={every}",
           "StripeOptions.reporter_poll_interval_seconds"),
        _s("billing", f"Run {replicas} replicas of billing.", {"replicas": replicas},
           f"replicas={replicas}", "Billing.replicas"),
    ]


def _monitoring(rng: random.Random) -> list[Setting]:
    days, prometheus = rng.choice((10, 45, 90)), rng.choice(("50Gi", "150Gi"))
    grafana, alerts = rng.choice(("10Gi", "20Gi")), rng.choice(("5Gi", "10Gi"))
    user = rng.choice(("ops", "grafana-admin", "sre"))
    return [
        _s("observability", f"Keep metrics for {days} days.", {"metrics_retention": f"{days}d"},
           f"metrics_retention='{days}d'", "Observability.metrics_retention"),
        _s("observability", f"Give Prometheus a {prometheus} volume.", {"prometheus_volume_size": prometheus},
           f"prometheus_volume_size={prometheus!r}", "Observability.prometheus_volume_size"),
        _s("observability", f"Give Grafana a {grafana} volume.", {"grafana_volume_size": grafana},
           f"grafana_volume_size={grafana!r}", "Observability.grafana_volume_size"),
        _s("observability", f"Give Alertmanager a {alerts} volume.", {"alertmanager_volume_size": alerts},
           f"alertmanager_volume_size={alerts!r}", "Observability.alertmanager_volume_size"),
        _s("observability", f"The Grafana admin user should be called {user}.", {"grafana_admin_user": user},
           f"grafana_admin_user={user!r}", "Observability.grafana_admin_user"),
        _s("observability", "Expose Grafana through a LoadBalancer service.",
           {"options": {"grafana_service_type": "LoadBalancer"}}, "grafana_service_type='LoadBalancer'",
           "KubePrometheusStackOptions.grafana_service_type"),
    ]


def _object_store(rng: random.Random) -> list[Setting]:
    filers, volumes = rng.choice((2, 3)), rng.choice((2, 4, 6))
    size, port = rng.choice(("50Gi", "200Gi", "500Gi")), rng.choice((8334, 9000))
    return [
        _s("seaweedfs", "Run 3 master servers.", {"master_replicas": 3}, "master_replicas=3",
           "SeaweedFSCluster.master_replicas"),
        _s("seaweedfs", f"Run {filers} filers.", {"filer_replicas": filers}, f"filer_replicas={filers}",
           "SeaweedFSCluster.filer_replicas"),
        _s("seaweedfs", f"It needs {volumes} volume servers of {size} each.",
           {"volume_replicas": volumes, "volume_size": size},
           [f"volume_replicas={volumes}", f"volume_size={size!r}"],
           "SeaweedFSCluster.volume_replicas", "SeaweedFSCluster.volume_size"),
        _s("seaweedfs", f"Serve S3 on port {port}.", {"s3_port": port}, f"s3_port={port}", "SeaweedFSCluster.s3_port"),
    ]


def _vault(rng: random.Random) -> list[Setting]:
    size, memory = rng.choice(("20Gi", "50Gi")), rng.choice(("1Gi", "2Gi"))
    return [
        _s("vault", f"Give each Vault server {size} for its data.", {"options": {"data_size": size}},
           f"data_size={size!r}", "VaultOptions.data_size"),
        _s("vault", "Run 5 Vault replicas.", {"options": {"replicas": 5}}, "replicas=5", "VaultOptions.replicas"),
        _s("vault", "Turn the Vault web UI off.", {"options": {"ui_enabled": False}}, "ui_enabled=False",
           "VaultOptions.ui_enabled"),
        _s("vault", "Enable the Vault agent injector.", {"options": {"injector_enabled": True}},
           "injector_enabled=True", "VaultOptions.injector_enabled"),
        _s("vault", f"Allow each Vault pod up to {memory} of memory.", {"options": {"memory_limit": memory}},
           f"memory_limit={memory!r}", "VaultOptions.memory_limit"),
    ]


def _gpu_plugin(rng: random.Random) -> list[Setting]:
    key, value = rng.choice((("gpu", "true"), ("accelerator", "nvidia"), ("node-type", "gpu")))
    runtime, version = rng.choice(("nvidia-cdi", "nvidia-legacy")), rng.choice(("0.17.4", "0.18.0"))
    return [
        _s("accelerator", f"Run it only on the nodes labelled {key}={value}.", {"node_selector": {key: value}},
           f"node_selector={{{key!r}: {value!r}}}", "Accelerator.node_selector"),
        _s("accelerator", f"It should use the runtime class {runtime}.", {"options": {"runtime_class_name": runtime}},
           f"runtime_class_name={runtime!r}", "DevicePluginOptions.runtime_class_name"),
        _s("accelerator", f"Install version {version} of the device plugin's chart.",
           {"options": {"chart_version": version}}, f"chart_version={version!r}", "DevicePluginOptions.chart_version"),
    ]


def _longhorn(rng: random.Random) -> list[Setting]:
    path = rng.choice(("/mnt/longhorn", "/data/longhorn"))
    return [
        _s("storage", "Keep 2 replicas of each Longhorn volume.", {"replica_count": 2}, "replica_count=2",
           "Storage.replica_count"),
        _s("storage", f"Longhorn should store its data under {path}.", {"options": {"data_path": path}},
           f"data_path={path!r}", "LonghornOptions.data_path"),
    ]


def _rke2(rng: random.Random) -> list[Setting]:
    cni = rng.choice(("calico", "flannel", "cilium"))
    version = rng.choice(("v1.32.5+rke2r1", "v1.31.9+rke2r1"))
    return [
        _s("cluster", f"Use {cni.capitalize()} for pod networking.", {"cni": cni}, f"cni={cni!r}", "RKE2Cluster.cni"),
        _s("cluster", f"Install RKE2 version {version}.", {"version": version}, f"version={version!r}",
           "RKE2Cluster.version"),
        _s("cluster", "Use Cilium for networking and let it replace kube-proxy.",
           {"cni": "cilium", "disable_kube_proxy": True}, ["cni='cilium'", "disable_kube_proxy=True"],
           "RKE2Cluster.cni", "RKE2Cluster.disable_kube_proxy"),
    ]


def _some(rng: random.Random, pool: list[Setting], count: int) -> list[Setting]:
    """`count` settings from `pool`, no two about the same field."""
    picked: list[Setting] = []
    for setting in rng.sample(pool, len(pool)):
        if len(picked) == count:
            break
        if not any(set(setting.fields) & set(p.fields) for p in picked):
            picked.append(setting)
    return picked


def _merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _apply(args: dict, settings: list[Setting]) -> dict:
    out = copy.deepcopy(args)
    for setting in settings:
        out[setting.component] = _merge(out[setting.component], setting.patch)
    return out


def _text(*sentences: str) -> str:
    return " ".join(s for s in sentences if s)


# ---- where the SDK defines a field, as the RAG service cuts it


@cache
def _sdk_classes() -> dict[str, type]:
    """Every SDK model a plan tool takes, by class name."""
    from pydantic import BaseModel

    from script_cases import tool_function

    found: dict[str, type] = {}

    def visit(annotation) -> None:
        if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
            if annotation.__name__ in found:
                return
            found[annotation.__name__] = annotation
            for info in annotation.model_fields.values():
                visit(info.annotation)
            for options in (getattr(annotation, "OPTIONS_FOR_TYPE", None) or {}).values():
                visit(options)
        for arg in typing.get_args(annotation):
            visit(arg)

    for hint in typing.get_type_hints(inspect.unwrap(tool_function(FULL_STACK))).values():
        visit(hint)
    return found


@cache
def _field_passages(path: str) -> dict[str, str]:
    """`Class.field` -> `path:start-end` for each field chunk of one file."""
    if str(REPO_ROOT / "api/microservices/rag/src") not in sys.path:
        sys.path.append(str(REPO_ROOT / "api/microservices/rag/src"))
    from corpus import chunk_file

    return {
        chunk.symbol: f"{path}:{chunk.start_line}-{chunk.end_line}"
        for chunk in chunk_file(REPO_ROOT / path, repo_root=REPO_ROOT)
        if chunk.kind == "field" and chunk.symbol
    }


def passage(field: str) -> str:
    """The passage RAG returns for the SDK field `Class.field`."""
    owner = _sdk_classes()[field.split(".")[0]]
    path = Path(inspect.getsourcefile(owner)).resolve().relative_to(REPO_ROOT).as_posix()
    return _field_passages(path)[field]


def _passages(settings: list[Setting]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(passage(field) for setting in settings for field in setting.fields))


def _lines(settings: list[Setting]) -> tuple[str, ...]:
    return tuple(line for setting in settings for line in setting.lines)


def _built(case_id: str, request: str, expected: dict, layers, settings: list[Setting], **more) -> ScriptCase:
    return ScriptCase(
        case_id,
        request,
        expected,
        tuple(layers),
        _lines(settings),
        held_out=True,
        reference_context_ids=_passages(settings),
        **more,
    )


# ---- build: on the request path


def _gateway_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    settings = _some(rng, [*_gateway(rng), *_controlplane(rng), *_postgres(rng), *_cache(rng)], rng.choice((1, 2)))
    opening = rng.choice(
        (
            "Add a model gateway in front of our models.",
            "We need the model gateway deployed.",
            "Set up a model gateway for our model traffic.",
            "Deploy the model gateway.",
        )
    )
    request = _text(opening, *(s.sentence for s in settings), *_request_path_says(rng, f))
    return _built(case_id, request, _apply(_request_path(f), settings), _REQUEST_PATH, settings)


def _tokenizer_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    settings = _some(rng, [*_tokenizer(rng), *_queue(rng)], rng.choice((1, 2)))
    opening = rng.choice(("Build a tokenizer for me.", "We need the tokenizer service.", "Add the tokenizer."))
    request = _text(opening, *(s.sentence for s in settings), *_request_path_says(rng, f, storage=True))
    return _built(case_id, request, _apply(_event_path(f), settings), _EVENT_PATH, settings)


def _enricher_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    settings = _some(rng, [*_enricher(rng), *_gateway(rng)[:4]], rng.choice((1, 2)))
    opening = rng.choice(("Add the usage enricher.", "Deploy the usage enricher.", "We need the usage enricher running."))
    request = _text(opening, *(s.sentence for s in settings), *_request_path_says(rng, f, storage=True))
    return _built(case_id, request, _apply(_event_path(f), settings), _EVENT_PATH, settings)


def _billing_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    secret = rng.choice(("billing-secrets", "stripe-billing-env", "billing-stripe"))
    settings = _some(rng, _billing(rng), rng.choice((1, 2)))
    opening = rng.choice(
        (
            "Add Stripe billing for usage.",
            "We want usage billed through Stripe.",
            "Deploy the billing service for Stripe.",
        )
    )
    args = {**_event_path(f), "billing": {"kubeconfig_path": WIRED, "existing_secret": secret}}
    facts = _request_path_says(rng, f, storage=True)
    facts.insert(rng.randrange(len(facts) + 1), f"Billing's secrets are in the Secret {secret}.")
    request = _text(opening, *(s.sentence for s in settings), *facts)
    return _built(case_id, request, _apply(args, settings), (*_EVENT_PATH, "billing"), settings)


# ---- build: rate limiting, with everything it needs given


def _limit(rng: random.Random) -> tuple[str, str, dict]:
    """(sentence, limiter type, limits) for one stated limit."""
    model = rng.choice(_ROUTED_MODELS)
    requests, tokens = rng.choice((30, 120, 600, 2000)), rng.choice((20000, 80000, 250000))
    return rng.choice(
        (
            (f"Limit each user to {requests} requests per minute.", "rpm", {"user_default": requests}),
            (f"Allow every model {requests} requests per minute.", "rpm", {"model_default": requests}),
            (f"Limit {model} to {requests} requests per minute.", "rpm", {"model_overrides": {model: requests}}),
            (f"Cap every user at {tokens} tokens per minute.", "tpm", {"user_default": tokens}),
            (f"Limit {model} to {tokens} tokens per minute.", "tpm", {"model_overrides": {model: tokens}}),
        )
    )


def _auth_url(rng: random.Random, f: Facts) -> str:
    return f"redis://:{_password(rng, 'cache')}@{f.cache}-valkey.valkey.svc:6379/0"


def _limiter_plan(f: Facts, kind: str, limits: dict, auth_url: str | None) -> tuple[dict, tuple[str, ...]]:
    """(the plan, its components) for a limiter of `kind`. A tokens-per-
    minute limiter counts from enriched events, so it brings the enricher
    and tokenizer."""
    policy = {"kubeconfig_path": WIRED, "type": kind, "cache_url": WIRED, "limits": limits}
    if auth_url:
        policy["cache_auth_url"] = auth_url
    base = _event_path(f) if kind == "tpm" else _with_queue(_request_path(f), f)
    layers = (*_EVENT_PATH, "policy") if kind == "tpm" else (*_REQUEST_PATH, "queue", "policy")
    return {**base, "policy": policy}, layers


def _limit_lines(kind: str, limits: dict) -> tuple[str, ...]:
    scope, value = next(iter(limits.items()))
    shown = f"{scope}={value!r}" if isinstance(value, dict) else f"{scope}={value}"
    return (f"type={kind!r}", shown)


def _limiter_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    sentence, kind, limits = _limit(rng)
    auth_url = _auth_url(rng, f)
    expected, layers = _limiter_plan(f, kind, limits, auth_url)
    facts = _request_path_says(rng, f, storage=True)
    facts.append(f"The cache's auth URL is {auth_url}.")
    return ScriptCase(
        case_id, _text(sentence, *facts), expected, layers, _limit_lines(kind, limits), held_out=True
    )


# ---- build: the admin portal


def _portal_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    secret = rng.choice(_ADMIN_SECRETS)
    replicas, port = rng.choice((2, 3, 4)), rng.choice((8080, 8090))
    setting = rng.choice(
        (
            _s("portal", f"Run {replicas} replicas of the portal.", {"replicas": replicas},
               f"replicas={replicas},", "Portal.replicas"),
            _s("portal", f"Serve the portal on port {port}.", {"service_port": port},
               f"service_port={port}", "Portal.service_port"),
        )
    )
    args = {
        "kubeconfig_path": f.kubeconfig,
        "cnpg": {
            "kubeconfig_path": WIRED,
            "name": f.db,
            "database": {"name": f.database, "owner": f.owner, "password": f.password},
        },
        "valkey": {"kubeconfig_path": WIRED, "name": f.cache},
        "controlplane": {"kubeconfig_path": WIRED, "type": "admin", "existing_secret": secret},
        "portal": {"kubeconfig_path": WIRED, "type": "admin"},
    }
    facts = [
        _kubeconfig_says(rng, f.kubeconfig),
        _cp_secret_says(rng, "admin", secret),
        _postgres_says(rng, f),
        _cache_says(rng, f.cache),
    ]
    rng.shuffle(facts)
    opening = rng.choice(("Add the admin portal.", "Deploy the admin portal for our operators.", "We need the admin portal."))
    request = _text(opening, setting.sentence, *facts)
    return _built(case_id, request, _apply(args, [setting]), ("valkey", "cnpg", "controlplane", "portal"), [setting])


# ---- build: one component with no dependencies, through its own tool


def _valkey_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    namespace = rng.choice(("caching", "platform-data", "shared"))
    setting = rng.choice(
        (
            *_cache(rng),
            _s("valkey", f"Put it in the namespace {namespace}.", {"namespace": namespace},
               f"namespace={namespace!r}", "Cache.namespace"),
        )
    )
    opening = rng.choice(
        (
            f"Add a Valkey cache called {f.cache}.",
            f"I need a Valkey cache named {f.cache}.",
            f"Deploy a Valkey cache; call it {f.cache}.",
        )
    )
    sentence = setting.sentence.replace("Install the cache", "Install it")
    args = _apply({"valkey": {"kubeconfig_path": f.kubeconfig, "name": f.cache}}, [setting])
    request = _text(opening, sentence, _kubeconfig_says(rng, f.kubeconfig))
    return _built(case_id, request, args, ("valkey",), [setting], tool="build_valkey_plan")


def _postgres_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    settings = _some(rng, _postgres(rng), rng.choice((1, 2)))
    opening = rng.choice(
        (
            f"Add a PostgreSQL cluster called {f.db} with a database {f.database} owned by {f.owner}, "
            f"password {f.password}.",
            f"We need a new Postgres cluster named {f.db}. Create the database {f.database} in it, owned by "
            f"{f.owner} with password {f.password}.",
            f"Deploy a Postgres cluster named {f.db} holding the database {f.database} (owner {f.owner}, "
            f"password {f.password}).",
        )
    )
    sentences = [
        s.sentence.replace(" of storage.", " of disk.").replace("the Postgres cluster", "it").replace("Run Postgres", "Run it")
        for s in settings
    ]
    args = {
        "cnpg": {
            "kubeconfig_path": f.kubeconfig,
            "name": f.db,
            "database": {"name": f.database, "owner": f.owner, "password": f.password},
        }
    }
    request = _text(opening, *sentences, _kubeconfig_says(rng, f.kubeconfig))
    return _built(case_id, request, _apply(args, settings), ("cnpg",), settings, tool="build_cnpg_plan")


def _serve_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    model = rng.choice(_MODELS)
    replicas, port = rng.choice((2, 3)), rng.choice((8001, 9000, 8080))
    context, cores, memory = rng.choice((2048, 8192, 16384)), rng.choice((4, 8, 12)), rng.choice((16, 24, 32))
    alias = rng.choice(("chat-small", "assistant", "internal-llm"))
    pool = [
        _s("inference", f"Run {replicas} replicas of it.", {"replicas": replicas}, f"replicas={replicas}",
           "Inference.replicas"),
        _s("inference", f"It should listen on port {port}.", {"port": port}, f"port={port}", "Inference.port"),
        _s("inference", f"Set its maximum context length to {context}.", {"max_model_len": context},
           f"max_model_len={context}", "Inference.max_model_len"),
        _s("inference", f"Give it {cores} CPU cores and {memory} GB of memory.",
           {"cpu_cores": cores, "memory_gb": memory}, [f"cpu_cores={cores}", f"memory_gb={memory}"],
           "Inference.cpu_cores", "Inference.memory_gb"),
        _s("inference", f"Clients should see the model under the name {alias}.", {"served_model_name": alias},
           f"served_model_name={alias!r}", "Inference.served_model_name"),
    ]
    settings = _some(rng, pool, rng.choice((1, 2)))
    opening = rng.choice((f"Serve {model} with vLLM.", f"Deploy {model} on vLLM.", f"I want {model} served by vLLM."))
    args = {"inference": {"kubeconfig_path": f.kubeconfig, "model": model}}
    request = _text(opening, *(s.sentence for s in settings), _kubeconfig_says(rng, f.kubeconfig))
    return _built(case_id, request, _apply(args, settings), ("inference",), settings, tool="build_inference_plan")


def _kserve_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    model = rng.choice(_MODELS)
    low, high = rng.choice(((0, 2), (1, 4), (0, 5), (2, 6)))
    setting = _s(
        "inference",
        f"It should scale between {low} and {high} replicas.",
        {"options": {"min_replicas": low, "max_replicas": high}},
        [f"min_replicas={low}", f"max_replicas={high}"],
        "KServeOptions.min_replicas", "KServeOptions.max_replicas",
    )
    opening = rng.choice((f"Serve {model} with KServe.", f"Deploy {model} through KServe."))
    args = {"inference": {"kubeconfig_path": f.kubeconfig, "model": model, "type": "kserve"}}
    request = _text(opening, setting.sentence, _kubeconfig_says(rng, f.kubeconfig))
    return _built(case_id, request, _apply(args, [setting]), ("inference",), [setting], tool="build_inference_plan")


def _gpu_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    settings = _some(rng, _gpu_plugin(rng), rng.choice((1, 2)))
    opening = rng.choice(
        ("Add the NVIDIA GPU device plugin.", "Install the NVIDIA device plugin so pods can request GPUs.")
    )
    args = {"accelerator": {"kubeconfig_path": f.kubeconfig}}
    request = _text(opening, *(s.sentence for s in settings), _kubeconfig_says(rng, f.kubeconfig))
    return _built(case_id, request, _apply(args, settings), ("accelerator",), settings, tool="build_accelerator_plan")


def _address_pool(rng: random.Random) -> str:
    net, start = rng.choice(("192.168.20", "10.10.5", "172.16.40")), rng.choice((200, 220, 240))
    return f"{net}.{start}-{net}.{start + rng.choice((5, 10, 15))}"


def _ingress_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    pool = _address_pool(rng)
    key, value = rng.choice((("role", "edge"), ("ingress", "true"), ("net", "public")))
    selector = _s("gateway", f"Only nodes labelled {key}={value} should announce those addresses.",
                  {"node_selector": {key: value}}, f"node_selector={{{key!r}: {value!r}}}",
                  "IngressGateway.node_selector")
    settings = [selector] if rng.random() < 0.5 else []
    opening = rng.choice(
        (
            f"Add a MetalLB and Istio ingress that hands out addresses from {pool}.",
            f"Set up the ingress gateway (MetalLB with Istio) using the address range {pool}.",
        )
    )
    args = {"gateway": {"kubeconfig_path": f.kubeconfig, "address_pool": [pool]}}
    request = _text(opening, *(s.sentence for s in settings), _kubeconfig_says(rng, f.kubeconfig))
    return ScriptCase(
        case_id,
        request,
        _apply(args, settings),
        ("ingress",),
        (f"{pool!r}", *_lines(settings)),
        held_out=True,
        reference_context_ids=(passage("IngressGateway.address_pool"), *_passages(settings)),
        tool="build_ingress_gateway_plan",
    )


# ---- a new cluster


@dataclass(frozen=True)
class Machines:
    name: str
    user: str
    key: str
    server: str
    agents: tuple[str, ...]


def _machines(rng: random.Random, *, agents: int | None = None) -> Machines:
    net = rng.choice(("10.0.3", "10.20.1", "192.168.50", "172.20.8"))
    first = rng.choice((11, 21, 31, 101))
    count = rng.choice((0, 1, 2, 3)) if agents is None else agents
    return Machines(
        rng.choice(_CLUSTER_NAMES),
        rng.choice(_SSH_USERS),
        rng.choice(_SSH_KEYS),
        f"{net}.{first}",
        tuple(f"{net}.{first + n}" for n in range(1, count + 1)),
    )


def _nodes(m: Machines) -> list[dict]:
    return [
        {"address": address, "user": m.user, "role": role, "ssh_key": m.key}
        for address, role in [(m.server, "server"), *((a, "agent") for a in m.agents)]
    ]


def _listed(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _machines_say(rng: random.Random, m: Machines) -> str:
    """The nodes of a new cluster and how to reach them."""
    if not m.agents:
        return rng.choice(
            (
                f"a single server node at {m.server}, reached as user {m.user} with the SSH key {m.key}",
                f"one server node at {m.server} that accepts SSH as {m.user} with the key {m.key}",
            )
        )
    access = rng.choice(
        (
            f"all reached as user {m.user} with the SSH key {m.key}",
            f"every node accepting SSH as {m.user} with the key {m.key}",
        )
    )
    agents = "agent nodes" if len(m.agents) > 1 else "an agent node"
    nodes = rng.choice(
        (
            f"a server node at {m.server} and {agents} at {_listed(m.agents)}",
            f"{m.server} as the server and {_listed(m.agents)} as "
            + ("agent nodes" if len(m.agents) > 1 else "the agent node"),
        )
    )
    return f"{nodes}, {access}"


def _cluster_case(rng: random.Random, case_id: str) -> ScriptCase:
    m = _machines(rng)
    settings = _some(rng, _rke2(rng), 1)
    opening = rng.choice(
        (
            f"Build a new RKE2 cluster called {m.name} with {_machines_say(rng, m)}.",
            f"Create a new RKE2 cluster named {m.name}: {_machines_say(rng, m)}.",
        )
    )
    args = {"cluster": {"name": m.name, "nodes": _nodes(m)}}
    request = _text(opening, *(s.sentence for s in settings))
    return _built(case_id, request, _apply(args, settings), ("cluster",), settings, tool="build_rke2_cluster_plan")


def _on_storage(rng: random.Random) -> tuple[str, str, dict, list[Setting]]:
    """One component that keeps its data on the cluster's storage:
    (what to call it in a request, its parameter, its arguments, a setting)."""
    name = rng.choice(("model-store", "artifacts", "weights", "datasets"))
    kind = rng.choice(("observability", "seaweedfs", "vault", "queue"))
    if kind == "observability":
        return "Prometheus and Grafana monitoring", kind, {"kubeconfig_path": WIRED, "storage_class": WIRED}, _monitoring(rng)
    if kind == "seaweedfs":
        return (
            f"SeaweedFS object storage called {name}",
            kind,
            {"kubeconfig_path": WIRED, "storage_class": WIRED, "name": name},
            _object_store(rng),
        )
    if kind == "vault":
        return "Vault for secrets management", kind, {"kubeconfig_path": WIRED}, _vault(rng)
    return "the NATS event queue", kind, {"kubeconfig_path": WIRED, "storage_class": WIRED}, _queue(rng)


def _stored_case(rng: random.Random, case_id: str) -> ScriptCase:
    """A component that needs storage, on a cluster that already has a
    StorageClass."""
    f = _facts(rng)
    what, param, args, pool = _on_storage(rng)
    settings = _some(rng, pool, rng.choice((1, 2)))
    opening = rng.choice((f"Add {what}.", f"We need {what}.", f"Deploy {what}."))
    expected = {"kubeconfig_path": f.kubeconfig, "existing_storage_class": f.storage_class, param: args}
    facts = [_kubeconfig_says(rng, f.kubeconfig), _storage_says(rng, f.storage_class)]
    rng.shuffle(facts)
    request = _text(opening, *(s.sentence for s in settings), *facts)
    return _built(case_id, request, _apply(expected, settings), (param,), settings)


def _new_cluster_case(rng: random.Random, case_id: str) -> ScriptCase:
    """A new cluster with a component that needs storage on it: Longhorn is
    part of the plan whether or not the request names it."""
    m = _machines(rng, agents=rng.choice((1, 2, 3)))
    what, param, args, pool = _on_storage(rng)
    named = rng.random() < 0.5
    settings = _some(rng, [*pool, *(_longhorn(rng) if named else [])], rng.choice((1, 2)))
    storage = ", with Longhorn for storage" if named or any(s.component == "storage" for s in settings) else ""
    request = _text(
        f"Build a new RKE2 cluster called {m.name} with {_machines_say(rng, m)}{storage}.",
        f"Then add {what} on it.",
        *(s.sentence for s in settings),
    )
    expected = {
        "cluster": {"name": m.name, "nodes": _nodes(m)},
        "storage": {"kubeconfig_path": WIRED, "type": "longhorn"},
        param: args,
    }
    return _built(case_id, request, _apply(expected, settings), ("storage", param), settings)


def _route_case(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    pool = _address_pool(rng)
    host = rng.choice(("chat.mcw.internal", "api.platform.example.net", "portal.corp.lan", "llm.apps.internal"))
    service, namespace = rng.choice((("chat-ui", "apps"), ("web-console", "frontend"), ("docs-site", "public")))
    port, name = rng.choice((8080, 3000, 8000)), rng.choice(("chat", "console", "docs", "web"))
    prefix = rng.choice(("/api", "/app", "/v2"))
    settings = (
        [_s("route", f"Only paths under {prefix} should be routed.", {"path_prefix": prefix},
            f"path_prefix={prefix!r}", "Route.path_prefix")]
        if rng.random() < 0.5
        else []
    )
    request = _text(
        f"Add an ingress that hands out addresses from {pool}, and route {host} to the service {service} "
        f"in namespace {namespace} on port {port}; call the route {name}.",
        *(s.sentence for s in settings),
        _kubeconfig_says(rng, f.kubeconfig),
    )
    expected = {
        "kubeconfig_path": f.kubeconfig,
        "ingress": {"kubeconfig_path": WIRED, "address_pool": [pool]},
        "route": {
            "kubeconfig_path": WIRED,
            "name": name,
            "namespace": namespace,
            "service": service,
            "port": port,
            "hostnames": [host],
        },
    }
    return ScriptCase(
        case_id,
        request,
        _apply(expected, settings),
        ("ingress", "route"),
        (f"hostnames=[{host!r}]", *_lines(settings)),
        held_out=True,
        reference_context_ids=(passage("Route.hostnames"), *_passages(settings)),
    )


# ---- ask: a value only the user can give is left out


def _ask_request_path(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    left_out, words = rng.choice(
        (
            ("model", ("upstream", "model server", "url")),
            ("password", ("password",)),
            ("gateway_secret", ("api_key_secret", "api key", "api-key")),
            ("cp_secret", ("existing_secret", "secret")),
            ("cache", ("valkey.name", "cache name", "name of the cache", "name for the cache", "cache")),
        )
    )
    expected = _request_path(f)
    if left_out == "model":
        del expected["gateway"]["upstream_url"]
    elif left_out == "password":
        del expected["cnpg"]["database"]["password"]
    elif left_out == "gateway_secret":
        del expected["gateway"]["api_key_secret"]
    elif left_out == "cp_secret":
        del expected["controlplane"]["existing_secret"]
    else:
        del expected["valkey"]["name"]
    opening = rng.choice(("Add a model gateway in front of our models.", "Deploy the model gateway."))
    request = _text(opening, *_request_path_says(rng, f, leave_out=left_out))
    return ScriptCase(case_id, request, expected, _REQUEST_PATH, held_out=True, ask_for=words)


def _ask_storage_class(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    what, param, args, _ = _on_storage(rng)
    request = _text(rng.choice((f"Add {what}.", f"Deploy {what}.")), _kubeconfig_says(rng, f.kubeconfig))
    return ScriptCase(
        case_id,
        request,
        {"kubeconfig_path": f.kubeconfig, param: args},
        (param,),
        held_out=True,
        ask_for=("storageclass", "storage class", "existing_storage_class"),
    )


def _ask_limiter_auth(rng: random.Random, case_id: str) -> ScriptCase:
    """A limiter beside the Valkey the plan builds: the chart turns auth on,
    and the policy notes say to ask for the cache's auth URL."""
    f = _facts(rng)
    sentence, kind, limits = _limit(rng)
    expected, layers = _limiter_plan(f, kind, limits, None)
    request = _text(sentence, *_request_path_says(rng, f, storage=True))
    return ScriptCase(
        case_id,
        request,
        expected,
        layers,
        held_out=True,
        ask_for=("cache_auth_url", "password", "auth"),
        asks_because="Otherwise ask them for it",
    )


def _ask_billing_secret(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    opening = rng.choice(("Add Stripe billing for usage.", "Deploy the billing service for Stripe."))
    request = _text(opening, *_request_path_says(rng, f, storage=True))
    return ScriptCase(
        case_id,
        request,
        {**_event_path(f), "billing": {"kubeconfig_path": WIRED}},
        (*_EVENT_PATH, "billing"),
        held_out=True,
        ask_for=("existing_secret", "billing secret", "secret"),
    )


def _ask_missing_part(rng: random.Random, case_id: str) -> ScriptCase:
    """A single component missing the one thing it can't be built without."""
    f = _facts(rng)
    m = _machines(rng)
    kind = rng.choice(("pool", "nodes", "cluster-name"))
    if kind == "pool":
        return ScriptCase(
            case_id,
            _text(rng.choice(("Add a MetalLB and Istio ingress.", "Set up the MetalLB and Istio ingress gateway.")),
                  _kubeconfig_says(rng, f.kubeconfig)),
            {"gateway": {"kubeconfig_path": f.kubeconfig}},
            ("ingress",),
            held_out=True,
            tool="build_ingress_gateway_plan",
            ask_for=("address", "ip range", "pool"),
        )
    if kind == "nodes":
        return ScriptCase(
            case_id,
            rng.choice((f"Build a new RKE2 cluster called {m.name}.", f"Create a new RKE2 cluster named {m.name}.")),
            {"cluster": {"name": m.name}},
            ("cluster",),
            held_out=True,
            tool="build_rke2_cluster_plan",
            ask_for=("node", "address"),
        )
    return ScriptCase(
        case_id,
        f"Build a new RKE2 cluster with {_machines_say(rng, m)}.",
        {"cluster": {"nodes": _nodes(m)}},
        ("cluster",),
        held_out=True,
        tool="build_rke2_cluster_plan",
        ask_for=("name",),
    )


def _ask_unsupported(rng: random.Random, case_id: str) -> ScriptCase:
    """A request for something the SDK does not accept: the right answer
    builds nothing and says what is supported."""
    f = _facts(rng)
    m = _machines(rng)
    # Not "queue": a replica count below the JetStream quorum floor is a
    # value the model can itself round up to a valid one, and a live run
    # showed it reasonably offering the user a choice between two valid
    # counts instead of a plain rejection -- a real, model-invented choice
    # with no fixed wording this harness's pause mechanism can pin down.
    # The two kinds below have no such middle ground: the SDK rejects them
    # outright, so the only right answers are "ask" or "say what's
    # supported", never a plan.
    kind = rng.choice(("enricher", "cni"))
    if kind == "enricher":
        replicas = rng.choice((2, 3, 4))
        expected = _event_path(f)
        expected["enricher"] = {**expected["enricher"], "replicas": replicas}
        return ScriptCase(
            case_id,
            _text(f"Add the usage enricher with {replicas} replicas.", *_request_path_says(rng, f, storage=True)),
            expected,
            _EVENT_PATH,
            held_out=True,
            ask_for=(
                "less than or equal to 1", "only one", "only 1", "single replica", "one replica", "1 replica",
                "at most one", "at most 1", "more than one", "more than 1", "<= 1", "≤ 1", "maximum of 1",
            ),
        )
    return ScriptCase(
        case_id,
        _text(f"Build a new RKE2 cluster called {m.name} with {_machines_say(rng, m)}.",
              "Use Weave Net for pod networking."),
        {"cluster": {"name": m.name, "cni": "weave", "nodes": _nodes(m)}},
        ("cluster",),
        held_out=True,
        tool="build_rke2_cluster_plan",
        ask_for=("canal", "calico", "flannel", "supported", "not one of"),
    )


# ---- pause: a decision is left open


def _pause_existing(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    opening = rng.choice(
        (
            f"Add a PostgreSQL cluster called {f.db} with a database {f.database} owned by {f.owner}, "
            f"password {f.password}.",
            f"Deploy a Postgres cluster named {f.db} holding the database {f.database} (owner {f.owner}, "
            f"password {f.password}).",
        )
    )
    spec = {"name": f.db, "database": {"name": f.database, "owner": f.owner, "password": f.password}}
    return ScriptCase(
        case_id,
        opening,
        {"cnpg": {"kubeconfig_path": f.kubeconfig, **spec}},
        ("cnpg",),
        (f"name={f.db!r}",),
        held_out=True,
        tool="build_cnpg_plan",
        pauses=((NEW_CLUSTER, EXISTING_CLUSTER),),
        answers=(EXISTING_CLUSTER, rng.choice((f"Its kubeconfig is {f.kubeconfig}.", f"The kubeconfig is at {f.kubeconfig}."))),
        first=("build_cnpg_plan", {"cnpg": spec}),
    )


def _pause_new(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    m = _machines(rng, agents=rng.choice((0, 1, 2)))
    return ScriptCase(
        case_id,
        rng.choice((f"Add a Valkey cache called {f.cache}.", f"I need a Valkey cache named {f.cache}.")),
        {
            "cluster": {"name": m.name, "nodes": _nodes(m)},
            "valkey": {"kubeconfig_path": WIRED, "name": f.cache},
        },
        ("valkey",),
        (f"name={f.cache!r}", f"name={m.name!r}"),
        held_out=True,
        pauses=((NEW_CLUSTER, EXISTING_CLUSTER),),
        answers=(NEW_CLUSTER, f"Call the cluster {m.name}, with {_machines_say(rng, m)}."),
        first=("build_valkey_plan", {"valkey": {"name": f.cache}}),
    )


def _open_limit(rng: random.Random) -> tuple[str, dict]:
    """A limit that doesn't say what it counts."""
    amount = rng.choice((40, 90, 300, 1500))
    return rng.choice(
        (
            (f"Limit each user to {amount} per minute.", {"user_default": amount}),
            (f"Cap every model at {amount} a minute.", {"model_default": amount}),
        )
    )


def _pause_limiter(rng: random.Random, case_id: str) -> ScriptCase:
    f = _facts(rng)
    sentence, limits = _open_limit(rng)
    kind = rng.choice(("rpm", "tpm"))
    auth_url = _auth_url(rng, f)
    expected, layers = _limiter_plan(f, kind, limits, auth_url)
    # What the request alone supports: the limiter with its type unset.
    first, _ = _limiter_plan(f, "rpm", limits, auth_url)
    del first["policy"]["type"]
    facts = _request_path_says(rng, f, storage=True)
    facts.append(f"The cache's auth URL is {auth_url}.")
    return ScriptCase(
        case_id,
        _text(sentence, *facts),
        expected,
        layers,
        _limit_lines(kind, limits),
        held_out=True,
        pauses=(("rpm", "tpm"),),
        answers=(kind,),
        first=(FULL_STACK, first),
    )


def _pause_typed(rng: random.Random, case_id: str) -> ScriptCase:
    """The cluster is left open and the user answers in their own words
    instead of picking an option."""
    f = _facts(rng)
    expected = _request_path(f)
    first = {k: v for k, v in expected.items() if k != "kubeconfig_path"}
    answer = rng.choice(
        (
            f"We already have one. Its kubeconfig is {f.kubeconfig}.",
            f"An existing cluster; the kubeconfig is at {f.kubeconfig}.",
        )
    )
    return ScriptCase(
        case_id,
        _text("Add a model gateway in front of our models.", *_request_path_says(rng, f, leave_out="kubeconfig")),
        expected,
        _REQUEST_PATH,
        (f"api_key_secret={f.gateway_secret!r}",),
        held_out=True,
        pauses=((NEW_CLUSTER, EXISTING_CLUSTER),),
        answers=(answer,),
        first=(FULL_STACK, first),
    )


def _pause_twice(rng: random.Random, case_id: str) -> ScriptCase:
    """Two decisions open at once: which cluster, and what the limit counts."""
    f = _facts(rng)
    sentence, limits = _open_limit(rng)
    kind = rng.choice(("rpm", "tpm"))
    auth_url = _auth_url(rng, f)
    expected, layers = _limiter_plan(f, kind, limits, auth_url)
    # The request says nothing about the cluster, its StorageClass included:
    # naming one would already say the cluster exists.
    first, _ = _limiter_plan(f, "rpm", limits, auth_url)
    del first["policy"]["type"]
    del first["kubeconfig_path"]
    del first["existing_storage_class"]
    facts = _request_path_says(rng, f, leave_out="kubeconfig")
    facts.append(f"The cache's auth URL is {auth_url}.")
    return ScriptCase(
        case_id,
        _text(sentence, *facts),
        expected,
        layers,
        _limit_lines(kind, limits),
        held_out=True,
        pauses=((NEW_CLUSTER, EXISTING_CLUSTER), ("rpm", "tpm")),
        answers=(
            f"An existing cluster; its kubeconfig is {f.kubeconfig} and its StorageClass is {f.storage_class}.",
            kind,
        ),
        first=(FULL_STACK, first),
    )


# What a set is made of: 27 build, 8 ask, 5 pause.
_PLAN = (
    *(("gateway", _gateway_case),) * 5,
    ("tokenizer", _tokenizer_case),
    *(("enricher", _enricher_case),) * 2,
    ("billing", _billing_case),
    *(("limiter", _limiter_case),) * 2,
    ("portal", _portal_case),
    ("valkey", _valkey_case),
    ("postgres", _postgres_case),
    *(("serve", _serve_case),) * 2,
    ("kserve", _kserve_case),
    ("gpu", _gpu_case),
    ("ingress", _ingress_case),
    ("cluster", _cluster_case),
    *(("stored", _stored_case),) * 4,
    *(("new-cluster", _new_cluster_case),) * 2,
    ("route", _route_case),
    *(("ask-value", _ask_request_path),) * 3,
    ("ask-storage", _ask_storage_class),
    ("ask-auth", _ask_limiter_auth),
    ("ask-billing", _ask_billing_secret),
    ("ask-part", _ask_missing_part),
    ("ask-unsupported", _ask_unsupported),
    ("pause-existing", _pause_existing),
    ("pause-new", _pause_new),
    ("pause-limiter", _pause_limiter),
    ("pause-typed", _pause_typed),
    ("pause-twice", _pause_twice),
)


@cache
def generate(seed: int = SEED) -> tuple[ScriptCase, ...]:
    """The set for `seed`. Each case draws from its own stream, so changing
    one kind of case leaves the others as they were."""
    return tuple(
        build(random.Random(f"{seed}/{n}/{kind}"), f"{kind}-{n + 1:02d}")
        for n, (kind, build) in enumerate(_PLAN)
    )


def main() -> None:
    """Prints a set: `python tests/eval/fresh_cases.py [seed]`."""
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else SEED
    for case in generate(seed):
        kind = "pause" if case.pauses else "ask" if case.asks else "build"
        print(f"[{case.id}] ({kind}) {case.request}")
        for answer in case.answers:
            print(f"    then: {answer}")


if __name__ == "__main__":
    main()
