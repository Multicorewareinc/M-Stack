"""Script-generation evaluation set: a request a user would type, and the
plan a correct agent builds for it.

Each case records the tool a correct plan calls and its arguments: the
whole-stack tool for anything with dependencies, a single-resource tool
for a component without any (the only tool the agent is offered for
such a request). The reference script is rendered from them by the tool
itself, so it is always valid SDK code and stays in step with the
renderer. Every request carries the values the tool needs from the user,
so a single turn can produce the whole script.

An ask case leaves out a value only the user can give, or asks for what
the platform can't deploy. Its arguments are what the user gave: the tool
rejects them, or, where the tool would build a plan that silently does
nothing, the skill notes say to ask first. The right answer is no plan
and a reply asking for the missing value or saying what is supported,
never a plan built on a made-up or unsupported one.

The rendered script spells out every field, defaults included, so a
generated script can be compared with the reference line by line.

A pause case (PAUSE_CASES) is a short conversation instead of one request:
the request leaves a decision open, the agent has to stop and offer the
options in `pauses`, and `answers` are what the user says next, one each
time a turn ends without a script. The expected plan is the one the whole
conversation asks for.
"""

from __future__ import annotations

from dataclasses import dataclass

KUBECONFIG = "/etc/rancher/rke2/rke2.yaml"
# A field the tool fills in itself, from the component it wires it to.
WIRED = "placeholder"
NATS_DEFAULT = "nats://nats.platform.svc.cluster.local:4222"

PLATFORM_VALUES = (
    f"The cluster's kubeconfig is {KUBECONFIG} and our models are served by vLLM at "
    "http://vllm.ai.svc:8000/v1. Gateway API keys are in the Secret gateway-keys and the "
    "organization control plane's secrets in org-cp-secrets. Call the Postgres cluster "
    "platform-db, with database appdb owned by appuser, password Kx7-plat-2291, and call "
    "the cache platform-cache."
)
STORAGE_CLASS = "Its StorageClass is longhorn."
STORAGE_VALUES = f"The cluster's kubeconfig is {KUBECONFIG}. {STORAGE_CLASS}"
CLUSTER = f"The cluster's kubeconfig is {KUBECONFIG}."
ADMIN_VALUES = (
    f"The cluster's kubeconfig is {KUBECONFIG} and the admin control plane's secrets are in "
    "admin-cp-secrets. Call the Postgres cluster admin-db, with database admindb owned by "
    "adminuser, password Rt5-adm-4402, and call the cache admin-cache."
)
NODES_TEXT = "all reached as user ubuntu with the SSH key ~/.ssh/id_ed25519"
SSH_KEY = "~/.ssh/id_ed25519"
FULL_STACK = "build_full_stack_plan"


def _nodes(server: str, *agents: str) -> list[dict]:
    return [
        {"address": address, "user": "ubuntu", "role": role, "ssh_key": SSH_KEY}
        for address, role in [(server, "server"), *((a, "agent") for a in agents)]
    ]


_BASELINE = {
    "kubeconfig_path": KUBECONFIG,
    "cnpg": {
        "kubeconfig_path": WIRED,
        "name": "platform-db",
        "database": {"name": "appdb", "owner": "appuser", "password": "Kx7-plat-2291"},
    },
    "valkey": {"kubeconfig_path": WIRED, "name": "platform-cache"},
    "controlplane": {
        "kubeconfig_path": WIRED,
        "type": "organization",
        "existing_secret": "org-cp-secrets",
    },
}
_GATEWAY = {
    "kubeconfig_path": WIRED,
    "upstream_url": "http://vllm.ai.svc:8000/v1",
    "api_key_secret": "gateway-keys",
    "org_cp_internal_url": WIRED,
}
_ENRICHER_CHAIN = {
    "existing_storage_class": "longhorn",
    "queue": {"kubeconfig_path": WIRED, "storage_class": WIRED},
    "tokenizer": {"kubeconfig_path": WIRED},
    "enricher": {"kubeconfig_path": WIRED, "event_backbone_url": NATS_DEFAULT},
    "gateway": {**_GATEWAY, "event_backbone_url": NATS_DEFAULT},
}
_REQUEST_PATH = ("valkey", "cnpg", "controlplane", "gateway")
_ENRICHER_PATH = (*_REQUEST_PATH, "queue", "tokenizer", "enricher")
_QUEUE = {"kubeconfig_path": WIRED, "storage_class": WIRED}
_RATE_LIMIT_CHAIN = {
    "existing_storage_class": "longhorn",
    "queue": _QUEUE,
    "gateway": {**_GATEWAY, "event_backbone_url": NATS_DEFAULT},
}


@dataclass(frozen=True)
class ScriptCase:
    id: str
    request: str
    # Arguments of a correct plan; for an ask case, what the user gave.
    expected: dict
    # Components the plan must contain: the dependency chain.
    layers: tuple[str, ...]
    # Lines the request asks for, as the tool renders them.
    must_set: tuple[str, ...] = ()
    # Scored only at the end, never looked at while tuning.
    held_out: bool = False
    # Passages RAG must return for the call to come out right, as RAG
    # reports them (path:start-end). Empty when no single passage decides
    # the call: a bare dependency chain comes from the agent's own rules.
    reference_context_ids: tuple[str, ...] = ()
    # The tool a correct plan calls.
    tool: str = FULL_STACK
    # Set for an ask case: words, any one of which the reply contains when
    # it asks for the missing value or says what is supported.
    ask_for: tuple[str, ...] = ()
    # For an ask case the tool accepts: the words of the skill-notes rule or
    # the source that says not to build it as asked, so a change to either
    # shows in the tests.
    asks_because: str = ""
    # A pause case: the options each pause must offer, in order, and what
    # the user says each time a turn ends without a script.
    pauses: tuple[tuple[str, ...], ...] = ()
    answers: tuple[str, ...] = ()
    # A pause case: the tool and arguments the request alone supports, the
    # call that has to end in the first pause.
    first: tuple[str, dict] | None = None

    @property
    def asks(self) -> bool:
        return bool(self.ask_for)

    @property
    def said(self) -> str:
        """Everything the user says in the case, the answers included."""
        return "\n".join([self.request, *self.answers])


SCRIPT_CASES: list[ScriptCase] = [
    ScriptCase(
        "chain-gateway",
        f"Add a model gateway in front of our models. {PLATFORM_VALUES}",
        {**_BASELINE, "gateway": _GATEWAY},
        _REQUEST_PATH,
    ),
    ScriptCase(
        "gateway-fail-open",
        "Add a model gateway in front of our models, and let requests through if the "
        f"rate-limit service can't be reached instead of rejecting them. {PLATFORM_VALUES}",
        {**_BASELINE, "gateway": {**_GATEWAY, "policy_fail_mode": "open"}},
        _REQUEST_PATH,
        ("policy_fail_mode='open'",),
        reference_context_ids=("multistack/gateway/spec.py:134-136",),
    ),
    ScriptCase(
        "gateway-own-registry",
        "Add a model gateway, pulling its image from our registry at "
        f"registry.internal/mcw/model-gateway. {PLATFORM_VALUES}",
        {
            **_BASELINE,
            "gateway": {
                **_GATEWAY,
                "options": {"image_repository": "registry.internal/mcw/model-gateway"},
            },
        },
        _REQUEST_PATH,
        ("image_repository='registry.internal/mcw/model-gateway'",),
        reference_context_ids=("multistack/gateway/spec.py:60-64",),
    ),
    ScriptCase(
        "gateway-slow-models",
        "Add a model gateway that gives slow models up to 2 minutes to answer before timing "
        f"out. {PLATFORM_VALUES}",
        {**_BASELINE, "gateway": {**_GATEWAY, "request_timeout": 120}},
        _REQUEST_PATH,
        ("request_timeout=120.0",),
        reference_context_ids=("multistack/gateway/spec.py:138-138",),
    ),
    ScriptCase(
        "gateway-model-routes",
        "Add a model gateway that sends llama-3.1-8b to http://vllm-llama.ai.svc:8000/v1 and "
        "qwen2.5-7b to http://vllm-qwen.ai.svc:8000/v1; everything else goes to our default "
        f"vLLM. {PLATFORM_VALUES}",
        {
            **_BASELINE,
            "gateway": {
                **_GATEWAY,
                "model_routes": {
                    "llama-3.1-8b": "http://vllm-llama.ai.svc:8000/v1",
                    "qwen2.5-7b": "http://vllm-qwen.ai.svc:8000/v1",
                },
            },
        },
        _REQUEST_PATH,
        (
            "'llama-3.1-8b': 'http://vllm-llama.ai.svc:8000/v1'",
            "'qwen2.5-7b': 'http://vllm-qwen.ai.svc:8000/v1'",
        ),
        reference_context_ids=("multistack/gateway/spec.py:121-123",),
    ),
    ScriptCase(
        "queue-volume",
        f"Add the NATS event queue with a 50Gi JetStream volume. {STORAGE_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "existing_storage_class": "longhorn",
            "queue": {
                "kubeconfig_path": WIRED,
                "storage_class": WIRED,
                "options": {"pvc_size": "50Gi"},
            },
        },
        ("queue",),
        ("pvc_size='50Gi'",),
        reference_context_ids=("multistack/queue/spec.py:130-130",),
    ),
    ScriptCase(
        "monitoring-retention",
        f"Add Prometheus and Grafana monitoring that keeps metrics for 30 days. {STORAGE_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "existing_storage_class": "longhorn",
            "observability": {
                "kubeconfig_path": WIRED,
                "storage_class": WIRED,
                "metrics_retention": "30d",
            },
        },
        ("observability",),
        ("metrics_retention='30d'",),
        reference_context_ids=("multistack/observability/spec.py:93-93",),
    ),
    ScriptCase(
        "chain-tokenizer",
        f"Build a tokenizer for me. {PLATFORM_VALUES} {STORAGE_CLASS}",
        {**_BASELINE, **_ENRICHER_CHAIN},
        _ENRICHER_PATH,
    ),
    ScriptCase(
        "tokenizer-encoding",
        "Build a tokenizer that counts tokens with the o200k_base encoding by default. "
        f"{PLATFORM_VALUES} {STORAGE_CLASS}",
        {
            **_BASELINE,
            **_ENRICHER_CHAIN,
            "tokenizer": {"kubeconfig_path": WIRED, "options": {"default_encoding": "o200k_base"}},
        },
        _ENRICHER_PATH,
        # The tokenizer service registers cl100k_base alone and won't start
        # with another default: the right answer says so and builds nothing.
        # Only the service's source says it -- the SDK takes any name and no
        # skill note mentions it -- so retrieval has to find it. This case
        # expected the o200k_base plan until RAG on turned up that registry
        # in the 2026-10-08 run.
        reference_context_ids=("api/microservices/tokenizer/src/service.py:23-25",),
        ask_for=("cl100k_base", "support"),
        asks_because='"cl100k_base": _encode_cl100k_base',
    ),
    ScriptCase(
        "enricher-tokenizer-timeout",
        "Add the usage enricher, and give the tokenizer at most 500 milliseconds to answer "
        f"before the enricher gives up on it. {PLATFORM_VALUES} {STORAGE_CLASS}",
        {
            **_BASELINE,
            **_ENRICHER_CHAIN,
            "enricher": {
                "kubeconfig_path": WIRED,
                "event_backbone_url": NATS_DEFAULT,
                "options": {"tokenizer_timeout_ms": 500},
            },
        },
        _ENRICHER_PATH,
        ("tokenizer_timeout_ms=500",),
        reference_context_ids=("multistack/enricher/spec.py:84-84",),
    ),
    # -- One component with no dependencies: its own tool is the only one offered.
    ScriptCase(
        "valkey-chart-version",
        f"Add a Valkey cache called session-cache, using version 2.4.1 of its Helm chart. {CLUSTER}",
        {
            "valkey": {
                "kubeconfig_path": KUBECONFIG,
                "name": "session-cache",
                "options": {"chart_version": "2.4.1"},
            }
        },
        ("valkey",),
        ("chart_version='2.4.1'",),
        reference_context_ids=("multistack/cache/spec.py:40-40",),
        tool="build_valkey_plan",
    ),
    ScriptCase(
        "postgres-instances",
        "Add a PostgreSQL cluster called orders-db running 2 instances, with a database orders "
        f"owned by orders_app, password Qp4-ord-7713. {CLUSTER}",
        {
            "cnpg": {
                "kubeconfig_path": KUBECONFIG,
                "name": "orders-db",
                "instances": 2,
                "database": {"name": "orders", "owner": "orders_app", "password": "Qp4-ord-7713"},
            }
        },
        ("cnpg",),
        ("instances=2",),
        reference_context_ids=("multistack/database/spec.py:202-202",),
        tool="build_cnpg_plan",
    ),
    ScriptCase(
        "cluster-cilium",
        "Build a new RKE2 cluster called lab with a server node at 10.0.0.11 and agent nodes at "
        f"10.0.0.12 and 10.0.0.13, {NODES_TEXT}, using Cilium for networking.",
        {
            "cluster": {
                "name": "lab",
                "cni": "cilium",
                "nodes": _nodes("10.0.0.11", "10.0.0.12", "10.0.0.13"),
            }
        },
        ("cluster",),
        ("cni='cilium'",),
        held_out=True,
        reference_context_ids=("multistack/core/rke2.py:60-62",),
        tool="build_rke2_cluster_plan",
    ),
    ScriptCase(
        "ingress-address-pool",
        "Add a MetalLB and Istio ingress that hands out addresses from "
        f"192.168.10.240-192.168.10.250. {CLUSTER}",
        {
            "gateway": {
                "kubeconfig_path": KUBECONFIG,
                "address_pool": ["192.168.10.240-192.168.10.250"],
            }
        },
        ("ingress",),
        ("'192.168.10.240-192.168.10.250'",),
        reference_context_ids=("multistack/ingress_gateway/spec.py:70-74",),
        tool="build_ingress_gateway_plan",
    ),
    ScriptCase(
        # Without "token": that word offers the tokenizer and the
        # whole-stack tool too, and "GPU" the device plugin.
        "serve-context-length",
        "Serve Qwen/Qwen2.5-1.5B-Instruct with vLLM, with a maximum context length of 8192. "
        f"{CLUSTER}",
        {
            "inference": {
                "kubeconfig_path": KUBECONFIG,
                "model": "Qwen/Qwen2.5-1.5B-Instruct",
                "max_model_len": 8192,
            }
        },
        ("inference",),
        ("max_model_len=8192",),
        reference_context_ids=("multistack/inference/spec.py:563-563",),
        tool="build_inference_plan",
    ),
    ScriptCase(
        "serve-on-cpu",
        "Serve TinyLlama/TinyLlama-1.1B-Chat-v1.0 with vLLM on CPU, with 8 cores and 16 GB of "
        f"memory. {CLUSTER}",
        {
            "inference": {
                "kubeconfig_path": KUBECONFIG,
                "model": "TinyLlama/TinyLlama-1.1B-Chat-v1.0",
                "cpu_cores": 8,
                "memory_gb": 16,
            }
        },
        ("inference",),
        ("cpu_cores=8", "memory_gb=16"),
        held_out=True,
        reference_context_ids=(
            "multistack/inference/spec.py:566-568",
            "multistack/inference/spec.py:569-569",
        ),
        tool="build_inference_plan",
    ),
    ScriptCase(
        "gpu-plugin-nodes",
        f"Add the NVIDIA GPU device plugin, only on the nodes labelled gpu=true. {CLUSTER}",
        {"accelerator": {"kubeconfig_path": KUBECONFIG, "node_selector": {"gpu": "true"}}},
        ("accelerator",),
        ("node_selector={'gpu': 'true'}",),
        reference_context_ids=("multistack/accelerator/spec.py:118-123",),
        tool="build_accelerator_plan",
    ),
    # -- The request path: gateway, control plane, Postgres, cache.
    ScriptCase(
        "gateway-replicas",
        f"Add a model gateway that runs 4 replicas. {PLATFORM_VALUES}",
        {**_BASELINE, "gateway": {**_GATEWAY, "replicas": 4}},
        _REQUEST_PATH,
        ("replicas=4",),
        reference_context_ids=("multistack/gateway/spec.py:140-140",),
    ),
    ScriptCase(
        "gateway-max-inflight",
        f"Add a model gateway that serves at most 200 requests at a time. {PLATFORM_VALUES}",
        {**_BASELINE, "gateway": {**_GATEWAY, "options": {"max_inflight": 200}}},
        _REQUEST_PATH,
        ("max_inflight=200",),
        reference_context_ids=("multistack/gateway/spec.py:71-71",),
    ),
    ScriptCase(
        "gateway-port",
        f"Add a model gateway that listens on port 9090. {PLATFORM_VALUES}",
        {**_BASELINE, "gateway": {**_GATEWAY, "service_port": 9090}},
        _REQUEST_PATH,
        ("service_port=9090",),
        held_out=True,
        reference_context_ids=("multistack/gateway/spec.py:141-141",),
    ),
    ScriptCase(
        "controlplane-replicas",
        f"Add a model gateway, with 3 replicas of the organization control plane. {PLATFORM_VALUES}",
        {
            **_BASELINE,
            "controlplane": {**_BASELINE["controlplane"], "replicas": 3},
            "gateway": _GATEWAY,
        },
        _REQUEST_PATH,
        ("replicas=3",),
        reference_context_ids=("multistack/controlplane/spec.py:135-135",),
    ),
    ScriptCase(
        "postgres-storage",
        f"Add a model gateway, and give its Postgres cluster 50Gi of storage. {PLATFORM_VALUES}",
        {**_BASELINE, "cnpg": {**_BASELINE["cnpg"], "storage_size": "50Gi"}, "gateway": _GATEWAY},
        _REQUEST_PATH,
        ("storage_size='50Gi'",),
        reference_context_ids=("multistack/database/spec.py:205-205",),
    ),
    ScriptCase(
        "admin-portal-replicas",
        f"Add the admin portal with 2 replicas. {ADMIN_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "cnpg": {
                "kubeconfig_path": WIRED,
                "name": "admin-db",
                "database": {"name": "admindb", "owner": "adminuser", "password": "Rt5-adm-4402"},
            },
            "valkey": {"kubeconfig_path": WIRED, "name": "admin-cache"},
            "controlplane": {
                "kubeconfig_path": WIRED,
                "type": "admin",
                "existing_secret": "admin-cp-secrets",
            },
            "portal": {"kubeconfig_path": WIRED, "type": "admin", "replicas": 2},
        },
        ("valkey", "cnpg", "controlplane", "portal"),
        ("replicas=2,",),
        held_out=True,
        reference_context_ids=("multistack/portal/spec.py:127-127",),
    ),
    # -- On an existing cluster, beside its StorageClass.
    ScriptCase(
        "queue-servers",
        f"Add the NATS event queue with 5 servers. {STORAGE_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "existing_storage_class": "longhorn",
            "queue": {**_QUEUE, "options": {"replicas": 5}},
        },
        ("queue",),
        ("replicas=5",),
        reference_context_ids=("multistack/queue/spec.py:124-128",),
    ),
    ScriptCase(
        "monitoring-prometheus-volume",
        f"Add Prometheus and Grafana monitoring with a 100Gi volume for Prometheus. {STORAGE_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "existing_storage_class": "longhorn",
            "observability": {
                "kubeconfig_path": WIRED,
                "storage_class": WIRED,
                "prometheus_volume_size": "100Gi",
            },
        },
        ("observability",),
        ("prometheus_volume_size='100Gi'",),
        reference_context_ids=("multistack/observability/spec.py:94-94",),
    ),
    ScriptCase(
        "monitoring-grafana-nodeport",
        "Add Prometheus and Grafana monitoring, with Grafana exposed as a NodePort service. "
        f"{STORAGE_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "existing_storage_class": "longhorn",
            "observability": {
                "kubeconfig_path": WIRED,
                "storage_class": WIRED,
                "options": {"grafana_service_type": "NodePort"},
            },
        },
        ("observability",),
        ("grafana_service_type='NodePort'",),
        held_out=True,
        reference_context_ids=("multistack/observability/spec.py:41-44",),
    ),
    ScriptCase(
        "seaweedfs-volumes",
        "Add SeaweedFS object storage called model-store, with 3 volume servers of 100Gi each. "
        f"{STORAGE_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "existing_storage_class": "longhorn",
            "seaweedfs": {
                "kubeconfig_path": WIRED,
                "storage_class": WIRED,
                "name": "model-store",
                "volume_replicas": 3,
                "volume_size": "100Gi",
            },
        },
        ("seaweedfs",),
        ("volume_replicas=3", "volume_size='100Gi'"),
        reference_context_ids=(
            "multistack/core/seaweedfs.py:106-106",
            "multistack/core/seaweedfs.py:108-108",
        ),
    ),
    # -- The event path: queue, tokenizer, enricher, limiters, billing.
    ScriptCase(
        "tokenizer-replicas",
        f"Build a tokenizer with 4 replicas. {PLATFORM_VALUES} {STORAGE_CLASS}",
        {
            **_BASELINE,
            **_ENRICHER_CHAIN,
            "tokenizer": {"kubeconfig_path": WIRED, "replicas": 4},
        },
        _ENRICHER_PATH,
        ("replicas=4",),
        reference_context_ids=("multistack/tokenizer/spec.py:91-91",),
    ),
    ScriptCase(
        "enricher-max-deliver",
        "Add the usage enricher, and let it try each event at most 10 times. "
        f"{PLATFORM_VALUES} {STORAGE_CLASS}",
        {
            **_BASELINE,
            **_ENRICHER_CHAIN,
            "enricher": {
                "kubeconfig_path": WIRED,
                "event_backbone_url": NATS_DEFAULT,
                "options": {"max_deliver": 10},
            },
        },
        _ENRICHER_PATH,
        ("max_deliver=10",),
        held_out=True,
        reference_context_ids=("multistack/enricher/spec.py:70-75",),
    ),
    ScriptCase(
        "rpm-per-user",
        f"Add rate limiting of 60 requests per minute per user. {PLATFORM_VALUES} {STORAGE_CLASS}",
        {
            **_BASELINE,
            **_RATE_LIMIT_CHAIN,
            "policy": {
                "kubeconfig_path": WIRED,
                "type": "rpm",
                "cache_url": WIRED,
                "limits": {"user_default": 60},
            },
        },
        (*_REQUEST_PATH, "queue", "policy"),
        # A limiter is composed beside the Valkey this plan builds, whose
        # chart turns auth on: without cache_auth_url it gets NOAUTH and
        # enforces nothing. The policy notes say to ask for it. This case
        # expected the plan without it until the 2026-10-08 run.
        ask_for=("cache_auth_url", "password", "auth"),
        asks_because="Otherwise ask them for it",
    ),
    ScriptCase(
        "rpm-per-model",
        "Add rate limiting of 1000 requests per minute for each model. "
        f"{PLATFORM_VALUES} {STORAGE_CLASS}",
        {
            **_BASELINE,
            **_RATE_LIMIT_CHAIN,
            "policy": {
                "kubeconfig_path": WIRED,
                "type": "rpm",
                "cache_url": WIRED,
                "limits": {"model_default": 1000},
            },
        },
        (*_REQUEST_PATH, "queue", "policy"),
        # A limiter is composed beside the Valkey this plan builds, whose
        # chart turns auth on: without cache_auth_url it gets NOAUTH and
        # enforces nothing. The policy notes say to ask for it. This case
        # expected the plan without it until the 2026-10-08 run.
        ask_for=("cache_auth_url", "password", "auth"),
        asks_because="Otherwise ask them for it",
    ),
    ScriptCase(
        "tpm-model-limit",
        f"Limit llama-3.1-8b to 50000 tokens per minute. {PLATFORM_VALUES} {STORAGE_CLASS}",
        {
            **_BASELINE,
            **_ENRICHER_CHAIN,
            "policy": {
                "kubeconfig_path": WIRED,
                "type": "tpm",
                "cache_url": WIRED,
                "limits": {"model_overrides": {"llama-3.1-8b": 50000}},
            },
        },
        (*_ENRICHER_PATH, "policy"),
        held_out=True,
        # A limiter is composed beside the Valkey this plan builds, whose
        # chart turns auth on: without cache_auth_url it gets NOAUTH and
        # enforces nothing. The policy notes say to ask for it. This case
        # expected the plan without it until the 2026-10-08 run.
        ask_for=("cache_auth_url", "password", "auth"),
        asks_because="Otherwise ask them for it",
    ),
    ScriptCase(
        "billing-meter-name",
        "Add Stripe billing for token usage, reporting to Stripe under the meter event name "
        "llm_tokens. Billing's secrets are in the Secret billing-secrets. "
        f"{PLATFORM_VALUES} {STORAGE_CLASS}",
        {
            **_BASELINE,
            **_ENRICHER_CHAIN,
            "billing": {
                "kubeconfig_path": WIRED,
                "existing_secret": "billing-secrets",
                "options": {"stripe_meter_event_name": "llm_tokens"},
            },
        },
        (*_ENRICHER_PATH, "billing"),
        ("stripe_meter_event_name='llm_tokens'",),
        reference_context_ids=("multistack/billing/spec.py:75-75",),
    ),
    # -- A new cluster: storage is composed, not named.
    ScriptCase(
        "new-cluster-monitoring",
        "Build a new RKE2 cluster called obs with a server node at 10.0.1.11 and agent nodes at "
        f"10.0.1.12 and 10.0.1.13, {NODES_TEXT}, with Longhorn storage and Prometheus and "
        "Grafana monitoring that keeps metrics for 7 days.",
        {
            "cluster": {"name": "obs", "nodes": _nodes("10.0.1.11", "10.0.1.12", "10.0.1.13")},
            "storage": {"kubeconfig_path": WIRED, "type": "longhorn"},
            "observability": {
                "kubeconfig_path": WIRED,
                "storage_class": WIRED,
                "metrics_retention": "7d",
            },
        },
        ("storage", "observability"),
        ("metrics_retention='7d'",),
        reference_context_ids=("multistack/observability/spec.py:93-93",),
    ),
    # -- The front door and a route through it.
    ScriptCase(
        "route-hostname",
        "Add an ingress that hands out addresses from 192.168.10.240-192.168.10.250, and route "
        "chat.mcw.internal to the service chat-ui in namespace apps on port 8080; call the route "
        f"chat. {CLUSTER}",
        {
            "kubeconfig_path": KUBECONFIG,
            "ingress": {
                "kubeconfig_path": WIRED,
                "address_pool": ["192.168.10.240-192.168.10.250"],
            },
            "route": {
                "kubeconfig_path": WIRED,
                "name": "chat",
                "namespace": "apps",
                "service": "chat-ui",
                "port": 8080,
                "hostnames": ["chat.mcw.internal"],
            },
        },
        ("ingress", "route"),
        ("hostnames=['chat.mcw.internal']",),
        held_out=True,
        reference_context_ids=("multistack/route/spec.py:131-134",),
    ),
    # -- Ask: a value only the user can give is missing.
    ScriptCase(
        "ask-model-server",
        "Add a model gateway in front of our models. "
        + PLATFORM_VALUES.replace(
            " and our models are served by vLLM at http://vllm.ai.svc:8000/v1", ""
        ),
        {
            **_BASELINE,
            "gateway": {
                "kubeconfig_path": WIRED,
                "api_key_secret": "gateway-keys",
                "org_cp_internal_url": WIRED,
            },
        },
        _REQUEST_PATH,
        ask_for=("upstream", "model server", "url"),
    ),
    ScriptCase(
        "ask-db-password",
        "Add a model gateway in front of our models. "
        + PLATFORM_VALUES.replace(", password Kx7-plat-2291", ""),
        {
            **_BASELINE,
            "cnpg": {
                "kubeconfig_path": WIRED,
                "name": "platform-db",
                "database": {"name": "appdb", "owner": "appuser"},
            },
            "gateway": _GATEWAY,
        },
        _REQUEST_PATH,
        held_out=True,
        ask_for=("password",),
    ),
    ScriptCase(
        "ask-storage-class",
        f"Add Prometheus and Grafana monitoring. {CLUSTER}",
        {
            "kubeconfig_path": KUBECONFIG,
            "observability": {"kubeconfig_path": WIRED, "storage_class": WIRED},
        },
        ("observability",),
        ask_for=("storageclass", "storage class", "existing_storage_class"),
    ),
    ScriptCase(
        "ask-address-pool",
        f"Add a MetalLB and Istio ingress. {CLUSTER}",
        {"gateway": {"kubeconfig_path": KUBECONFIG}},
        ("ingress",),
        held_out=True,
        tool="build_ingress_gateway_plan",
        ask_for=("address", "ip range", "pool"),
    ),
    ScriptCase(
        "ask-cluster-nodes",
        "Build a new RKE2 cluster called edge.",
        {"cluster": {"name": "edge"}},
        ("cluster",),
        tool="build_rke2_cluster_plan",
        ask_for=("node", "address"),
    ),
    # -- New since the KServe v0.20 upgrade and the Vault capability --
    # none of these were used while tuning anything, so all three are
    # held out by construction, not just by the held_out flag.
    ScriptCase(
        "vault-non-ha",
        f"Add a single-pod, non-HA Vault for secrets management -- just a lab instance, "
        f"nothing real stored in it yet. {STORAGE_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "existing_storage_class": "longhorn",
            "vault": {
                "kubeconfig_path": WIRED,
                "options": {"ha_enabled": False, "replicas": 1},
            },
        },
        ("vault",),
        ("ha_enabled=False", "replicas=1"),
        held_out=True,
        reference_context_ids=(
            "multistack/vault/spec.py:91-93",
            "multistack/vault/spec.py:94-97",
        ),
    ),
    ScriptCase(
        "vault-ha-replicas",
        f"Add Vault for secrets management, with 5 Raft replicas. {STORAGE_VALUES}",
        {
            "kubeconfig_path": KUBECONFIG,
            "existing_storage_class": "longhorn",
            "vault": {"kubeconfig_path": WIRED, "options": {"replicas": 5}},
        },
        ("vault",),
        ("replicas=5",),
        held_out=True,
        reference_context_ids=("multistack/vault/spec.py:94-97",),
    ),
    ScriptCase(
        "inference-kserve-scale-to-zero",
        f"Serve Qwen/Qwen2.5-1.5B-Instruct with KServe, scaling from 0 up to 3 replicas. {CLUSTER}",
        {
            "inference": {
                "kubeconfig_path": KUBECONFIG,
                "model": "Qwen/Qwen2.5-1.5B-Instruct",
                "type": "kserve",
                "options": {"min_replicas": 0, "max_replicas": 3},
            },
        },
        ("inference",),
        ("min_replicas=0", "max_replicas=3"),
        held_out=True,
        reference_context_ids=(
            "multistack/inference/spec.py:356-362",
            "multistack/inference/spec.py:363-363",
        ),
        tool="build_inference_plan",
    ),
]


NEW_CLUSTER = "Create a new cluster"
EXISTING_CLUSTER = "Use an existing cluster"
_CACHE_AUTH_URL = "redis://:Vk81-cache@platform-cache-valkey.valkey.svc:6379/0"
_RATE_LIMIT_PLAN = {
    **_BASELINE,
    **_RATE_LIMIT_CHAIN,
    "policy": {
        "kubeconfig_path": WIRED,
        "cache_url": WIRED,
        "cache_auth_url": _CACHE_AUTH_URL,
        "limits": {"user_default": 60},
    },
}

PAUSE_CASES: list[ScriptCase] = [
    ScriptCase(
        "pause-existing-cluster",
        "Add a Valkey cache called sessions.",
        {"valkey": {"kubeconfig_path": KUBECONFIG, "name": "sessions"}},
        ("valkey",),
        ("name='sessions'",),
        tool="build_valkey_plan",
        pauses=((NEW_CLUSTER, EXISTING_CLUSTER),),
        answers=(EXISTING_CLUSTER, f"Its kubeconfig is {KUBECONFIG}."),
        first=("build_valkey_plan", {"valkey": {"name": "sessions"}}),
    ),
    ScriptCase(
        "pause-new-cluster",
        "Add a Valkey cache called sessions.",
        {
            "cluster": {"name": "edge", "nodes": _nodes("10.0.2.11")},
            "valkey": {"kubeconfig_path": WIRED, "name": "sessions"},
        },
        ("valkey",),
        ("name='sessions'", "name='edge'"),
        pauses=((NEW_CLUSTER, EXISTING_CLUSTER),),
        answers=(
            NEW_CLUSTER,
            f"Call the cluster edge, with one server node at 10.0.2.11, reached as user ubuntu "
            f"with the SSH key {SSH_KEY}.",
        ),
        first=("build_valkey_plan", {"valkey": {"name": "sessions"}}),
    ),
    ScriptCase(
        "pause-limiter-type",
        # "60 per minute" of what: requests or tokens is the user's to say.
        f"Limit each user to 60 per minute. {PLATFORM_VALUES} {STORAGE_CLASS} The cache's auth "
        f"URL is {_CACHE_AUTH_URL}.",
        {**_RATE_LIMIT_PLAN, "policy": {**_RATE_LIMIT_PLAN["policy"], "type": "rpm"}},
        (*_REQUEST_PATH, "queue", "policy"),
        ("type='rpm'", "user_default=60"),
        pauses=(("rpm", "tpm"),),
        answers=("rpm",),
        first=(FULL_STACK, _RATE_LIMIT_PLAN),
    ),
]

# What the runner runs and the scorers score.
ALL_CASES: list[ScriptCase] = [*SCRIPT_CASES, *PAUSE_CASES]


def tool_function(name: str):
    """The agent's plan tool called `name`."""
    import importlib
    import pkgutil

    import mcp_tools

    for module in pkgutil.iter_modules(mcp_tools.__path__):
        found = getattr(importlib.import_module(f"mcp_tools.{module.name}"), name, None)
        if callable(found):
            return found
    raise LookupError(f"no plan tool named {name}")


def reference_plan(case: ScriptCase) -> dict:
    """The tool's result for the case's arguments, through the same checks
    and filling the agent applies before calling it: a valid plan, or for
    an ask case the rejection naming what the user must still give."""
    args = case.expected
    if case.tool == FULL_STACK:
        from mcp_tools.full_stack import against_the_user, missing_dependencies, prepare_args

        error = missing_dependencies(args) or against_the_user(args, case.request)
        if error:
            return {"valid": False, "error": error}
        args = prepare_args(args)
    try:
        return tool_function(case.tool)(**args)
    except ValueError as e:
        # The SDK types reject the arguments before the tool's body runs.
        return {"valid": False, "error": str(e)}


def reference_script(case: ScriptCase) -> str:
    result = reference_plan(case)
    if not result.get("valid"):
        raise ValueError(f"{case.id}: the expected plan does not validate: {result.get('error')}")
    return result["script"]
