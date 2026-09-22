"""The TPM limiter, installed from its chart through the Helm layer.

The RPM driver's sibling, and deliberately close to it — same contract,
same Helm layer, same single-replica constraint. What differs is all
about TPM needing a token *count* per event where RPM needs only the
event:

  * it consumes `response` events, not `request` events, so a request is
    charged after the fact rather than on arrival, off a *different*
    stream from RPM's raw one — the `enricher` service's derived
    `gateway.events.enriched` (ADR-030), which is what guarantees every
    event has a usable count; TPM itself has no tokenizer dependency;
  * its limits are token counts, which are three or four orders of
    magnitude larger than the request counts that look identical in YAML.

That second one is a way to end up with a limiter that is healthy and not
limiting much, so it gets a prerequisite warning rather than a comment.
"""
from typing import Any, Dict, List

from ..base import PolicyError, PolicyPrerequisiteError
from ..spec import Policy
from ...helm import HelmRunner
from ...kube import kubectl, require_cli, require_cluster

# Below this, a token ceiling is almost certainly a request ceiling that
# was copied from an RPM limits document. A 0.5B model's single reply is
# already ~100 tokens, so 1000/minute would deny a second request.
SUSPICIOUSLY_SMALL_TOKEN_LIMIT = 1000

# What the service logs when its consumer is not counting. Three separate
# causes, and the driver has to know all of them: `enforcing()` exists to
# answer "is it limiting", and a pod that failed to bind its durable
# answers /check exactly like one that never reached NATS.
INACTIVE_MARKERS = (
    # Backbone unreachable at connect. The service retries in the background with
    # exponential backoff (_retry_connect()) rather than giving up, so this fires once per
    # failed attempt and clears on its own once NATS comes back -- but at each log line the
    # consumer is, right then, not bound and not counting, which is exactly what this checks.
    "tpm_consumer_connect_failed_retrying",
    "tpm_consumer_durable_conflict",          # another pod holds the durable
    "tpm_consumer_bind_failed",               # transient, retried on reconnect
)


class TPMDriver:
    def __init__(self, ready_timeout: int = 300) -> None:
        self._ready_timeout = ready_timeout

    def _helm(self, policy: Policy) -> HelmRunner:
        return HelmRunner(policy.kubeconfig_path, error_cls=PolicyError)

    def _values(self, policy: Policy) -> Dict[str, Any]:
        options = policy.options
        values: Dict[str, Any] = {
            "replicaCount": policy.replicas,
            "config": {
                "valkeyUrl": policy.cache_url,
                "allowNoBackbone": policy.allow_no_backbone,
                "adminCpUrl": policy.admin_cp_url,
                "events": {
                    "backboneUrl": policy.event_backbone_url,
                    "streamName": options.event_stream_name,
                    "streamSubject": options.event_stream_subject,
                    "durableName": options.durable_name,
                    "dedupeTtl": options.dedupe_ttl,
                },
            },
            # The chart takes the limits structured and assembles the JSON
            # itself, into a Secret — the override maps are keyed by
            # bearer token.
            "secret": {"limits": {
                "userDefault": policy.limits.user_default,
                "modelDefault": policy.limits.model_default,
                "userOverrides": policy.limits.user_overrides,
                "modelOverrides": policy.limits.model_overrides,
                "userModelOverrides": policy.limits.user_model_overrides,
            }},
        }
        if policy.cache_auth_url is not None:
            # Into the Secret, not config.valkeyUrl: that renders into the
            # ConfigMap, where a password is readable by anything holding
            # `get configmaps` here. The chart's envFrom applies the Secret
            # after the ConfigMap, so this wins inside the pod.
            values["secret"]["valkeyUrl"] = \
                policy.cache_auth_url.get_secret_value()
        if policy.admin_cp_service_api_key is not None:
            values["secret"]["serviceApiKey"] = \
                policy.admin_cp_service_api_key.get_secret_value()
        if policy.network_policy_allowed_ingress:
            values["networkPolicy"] = {
                "enabled": True,
                "allowedIngress": policy.network_policy_allowed_ingress,
            }
        if options.image_tag:
            values["image"] = {"tag": options.image_tag}
        # Absent leaves the chart's own default in place; an explicit {}
        # overrides it with "schedule anywhere", which is a different
        # statement and sometimes the intended one.
        if policy.node_selector is not None:
            values["nodeSelector"] = policy.node_selector
        if policy.tolerations is not None:
            values["tolerations"] = policy.tolerations
        return values

    def check_prerequisites(self, policy: Policy) -> List[str]:
        warnings: List[str] = []
        require_cli("helm", error_cls=PolicyPrerequisiteError)
        require_cluster(policy.kubeconfig_path, capability="a policy service")

        if policy.allow_no_backbone:
            warnings.append(
                "allow_no_backbone=True: /check will answer and allow every "
                "request, because nothing increments the counters. Deliberate "
                "here, but it is not rate limiting."
            )
        limits = policy.limits
        if not any((limits.user_default, limits.model_default,
                    limits.user_overrides, limits.model_overrides,
                    limits.user_model_overrides)):
            warnings.append(
                "every limit is 0, which means unlimited rather than blocked "
                "— this deploys a service that denies nothing."
            )
        else:
            # Only the defaults, not the override maps: a deliberately
            # tiny per-model ceiling is a plausible thing to want, while
            # a tiny *default* is nearly always a copied RPM number.
            small = {
                name: value
                for name, value in (("user_default", limits.user_default),
                                    ("model_default", limits.model_default))
                if 0 < value < SUSPICIOUSLY_SMALL_TOKEN_LIMIT
            }
            if small:
                warnings.append(
                    f"{small} — these are token counts per minute, not "
                    f"request counts. Under "
                    f"{SUSPICIOUSLY_SMALL_TOKEN_LIMIT} a single completion "
                    f"can exhaust the window, so this denies almost "
                    f"everything. If these came from an rpm limits "
                    f"document, multiply them by the tokens a typical "
                    f"reply costs."
                )
        return warnings

    def create(self, policy: Policy) -> str:
        self._helm(policy).install_or_upgrade(
            policy.options.release_name,
            chart=policy.options.chart,
            chart_version=policy.options.chart_version,
            namespace=policy.resolved_namespace,
            values=self._values(policy),
            # Our own chart declares every value it reads, so an
            # unrecognised path is a mistake rather than a chart quirk.
            strict_values=True,
        )
        return policy.endpoint

    def delete(self, policy: Policy) -> None:
        self._helm(policy).uninstall(
            policy.options.release_name,
            namespace=policy.resolved_namespace,
            missing_ok=True,
        )

    def enforcing(self, policy: Policy) -> bool:
        """Whether the counting consumer is actually attached.

        Answering /check is not the same as limiting. The consumer binds
        to a durable name and JetStream allows one subscription per
        durable, so a pod that lost the race — a rolling update, a second
        replica — serves happily and counts nothing.

        Readiness is the authoritative answer here, unlike for rpm. The
        service has a `/ready` that returns 503 when a backbone is
        configured and the consumer is not bound (ADR-016), and this
        chart probes it — so a pod that is Ready *is* counting, and
        `readyReplicas` answers the question without parsing logs.

        The log markers stay as a fallback, because an image built before
        `/ready` existed will pass a readiness probe it never sees.
        """
        if not policy.event_backbone_url:
            return False

        ready = kubectl(
            policy.kubeconfig_path, "get", "deploy", policy.service_name,
            "-n", policy.resolved_namespace,
            "-o", "jsonpath={.status.readyReplicas}",
            error_cls=PolicyError, check=False,
        )
        # Empty means the field is absent, which Kubernetes does when the
        # count is zero — so "" and "0" are the same answer.
        if not (ready or "").strip().strip("0"):
            return False

        logs = kubectl(
            policy.kubeconfig_path, "logs",
            f"deploy/{policy.service_name}",
            "-n", policy.resolved_namespace, "--tail=200",
            error_cls=PolicyError, check=False,
        ) or ""
        return not any(marker in logs for marker in INACTIVE_MARKERS)
