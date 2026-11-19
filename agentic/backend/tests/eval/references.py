"""The correct answer to each grounding question, for RAGAS.

cases.py records the one string a correct answer must contain; RAGAS's
context recall and factual correctness need a full reference answer to
break into claims. Each is written from the SDK and service source, with
the default values the code has today. Keyed by GroundingCase.id.
"""

from __future__ import annotations

REFERENCES: dict[str, str] = {
    "ground-enricher-publish": (
        "The enricher republishes enriched events to the gateway.events.enriched subject, "
        "on the separate GATEWAY_EVENTS_ENRICHED stream."
    ),
    "ground-tokenizer-port": (
        "The tokenizer listens on port 8000 by default, set by Tokenizer.service_port."
    ),
    "ground-queue-replicas": (
        "The NATS queue runs 3 replicas by default, set by NatsQueueOptions.replicas."
    ),
    "ground-policy-admin-cp": (
        "Set Policy.admin_cp_url to the admin control plane's internal URL, and "
        "admin_cp_service_api_key to that control plane's SERVICE_API_KEY, so the limiter "
        "reads per-organization plan limits from it."
    ),
    "ground-cache-auth": (
        "A rate limiter fails open when it cannot read its counters from Valkey or Redis: "
        "it allows the request instead of blocking it. A common cause is a cache with "
        "authentication enabled while cache_auth_url, the cache URL with its password, is "
        "unset, so every request gets NOAUTH and the limiter enforces nothing while "
        "staying Ready."
    ),
    "ground-billing-password": (
        "No. The database password is a real credential and must never be invented; ask "
        "the user for it."
    ),
    "ground-image-repo-gateway": (
        "Set image_repository in ModelGatewayOptions to the full image path in your "
        "registry. Left unset, the chart's own default registry is used."
    ),
    "ground-image-repo-billing": (
        "Set image_repository in StripeOptions, the billing options, to the full image "
        "path in your registry. Left unset, the chart's own default registry is used."
    ),
    "ground-llamacpp-gpu": (
        "LlamaCppOptions.n_gpu_layers controls how many layers llama.cpp offloads to the "
        "GPU. The default is 99, which offloads every layer; 0 keeps the model on the CPU."
    ),
    "ground-llamacpp-image": (
        "The default llama.cpp image is ghcr.io/ggml-org/llama.cpp, pinned by digest "
        "(@sha256:93e3b8da7ce2d7c501e826e3c87b6fde980fac18a8058d38e24529a5b3b83b94)."
    ),
    "ground-billing-poll": (
        "The billing reporter polls for new usage every 5 seconds by default, set by "
        "StripeOptions.reporter_poll_interval_seconds."
    ),
    "ground-enricher-replicas-limit": (
        "No. The enricher is limited to one replica (replicas has le=1), because it is a "
        "durable JetStream consumer and only one subscription can bind to it."
    ),
    "ground-gateway-fail-mode": (
        "It follows Gateway.policy_fail_mode, which defaults to closed: a policy error "
        "blocks the request. Set it to open to let requests through when the policy "
        "service cannot be reached."
    ),
    "ground-gateway-timeout": (
        "The gateway's default upstream request timeout is 60 seconds, set by "
        "Gateway.request_timeout."
    ),
    "ground-gateway-routes": (
        "Use Gateway.model_routes, a mapping from model name to upstream URL; requests for "
        "a mapped model go to that URL. Credentials for a route belong in the Secret, not "
        "in the mapping."
    ),
    "ground-gateway-ratelimit-headers": (
        "The gateway stamps X-RateLimit headers on responses: X-RateLimit-Limit, "
        "X-RateLimit-Remaining and X-RateLimit-Reset, plus per-scope "
        "X-RateLimit-<name>-Limit, -Remaining and -Reset headers."
    ),
    "ground-billing-meter": (
        "Billing reports usage to Stripe under the meter event name tokens_used, the "
        "default of StripeOptions.stripe_meter_event_name."
    ),
    "ground-billing-retries": (
        "The billing reporter tries a failed usage report up to 5 times by default, set by "
        "StripeOptions.reporter_max_attempts."
    ),
    "ground-billing-batch": (
        "The billing reporter sends up to 50 usage events at once by default, set by "
        "StripeOptions.reporter_batch_size."
    ),
    "ground-billing-backoff": (
        "The billing reporter waits 30 seconds before its first retry by default "
        "(StripeOptions.reporter_backoff_base_seconds), backing off to at most 3600 seconds "
        "(reporter_backoff_max_seconds)."
    ),
    "ground-enricher-timeout": (
        "The enricher waits 2000 milliseconds for the tokenizer by default, set by "
        "EnricherOptions.tokenizer_timeout_ms."
    ),
    "ground-enricher-redeliver": (
        "An event is delivered to the enricher up to 5 times by default, set by "
        "EnricherOptions.max_deliver."
    ),
    "ground-enricher-ack": (
        "The enricher has 30 seconds to acknowledge an event by default, set by "
        "EnricherOptions.ack_wait_seconds."
    ),
    "ground-inference-cpu-image": (
        "On a CPU-only node vLLM uses the vllm/vllm-openai-cpu image, "
        "vllm/vllm-openai-cpu:v0.28.0-x86_64 by default."
    ),
    "ground-inference-cache-path": (
        "llama.cpp keeps downloaded model files in a hostPath on the node, "
        "/var/lib/llamacpp-models by default, set by LlamaCppOptions.model_cache_path."
    ),
    "ground-inference-service-type": (
        "llama.cpp is exposed as a NodePort Service by default, set by "
        "LlamaCppOptions.service_type."
    ),
    "ground-inference-batch": (
        "llama.cpp uses a micro-batch size of 256 (LlamaCppOptions.ubatch_size) and a batch "
        "size of 512 (batch_size) by default."
    ),
    "ground-queue-volume": (
        "The JetStream volume is 20Gi by default, set by NatsQueueOptions.pvc_size."
    ),
    "ground-queue-file-store": (
        "JetStream can use 15Gi of file storage by default, set by "
        "NatsQueueOptions.file_store_max_size."
    ),
    "ground-route-match": ("A route's path_match_type can be PathPrefix, the default, or Exact."),
    "ground-route-class": (
        "An HTTP route's parent Gateway asks for the istio gateway class by default, set by "
        "HTTPRouteOptions.gateway_class_name."
    ),
    "ground-accelerator-runtime": (
        "The NVIDIA device plugin needs the nvidia runtime class, the default of "
        "DevicePluginOptions.runtime_class_name."
    ),
    "ground-controlplane-peer": (
        "A control plane waits 3000 milliseconds for its peer by default, set by "
        "ControlPlane.peer_timeout_ms."
    ),
    "ground-controlplane-seed": (
        "On first install the admin control plane creates three plans: Free (10,000 TPM, "
        "60 RPM, 1,000,000 monthly tokens), Pro (100,000 TPM, 600 RPM, 50,000,000 monthly "
        "tokens) and Enterprise (0 for all three). This is controlled by "
        "AdminControlPlaneOptions.seed_plans, true by default; a plan that already exists is "
        "never overwritten."
    ),
    "ground-observability-retention": (
        "Prometheus keeps metrics for 15 days by default, set by "
        "Observability.metrics_retention (15d)."
    ),
    "ground-storage-replicas": (
        "Longhorn keeps 3 copies of each volume by default, set by Storage.replica_count."
    ),
    "ground-portal-resolver": (
        "The portal's nginx uses the DNS resolver 10.43.0.10 by default, set by Portal.resolver."
    ),
    "ground-tokenizer-encoding": (
        "The tokenizer uses the cl100k_base encoding by default, set by "
        "TiktokenOptions.default_encoding."
    ),
    "ground-policy-dedupe": (
        "The rate limiter's duplicate event window is 120 seconds by default, set by "
        "dedupe_ttl in its RPM or TPM options, with a minimum of 60."
    ),
    "ground-ingress-pool": (
        "Give MetalLB its address range through IngressGateway.address_pool, a list of "
        "address ranges such as 192.168.1.240-192.168.1.250."
    ),
    "ground-valkey-endpoint": (
        "The Valkey cache produces redis://<service>.<namespace>.svc.cluster.local:6379/0, "
        "published to the stack as cache_url. It carries no password; a cache with "
        "authentication needs cache_auth_url for that."
    ),
}
