"""The RPM limiter, installed from its chart through the Helm layer.

Every chart value this renders was once a default in the chart's own
values.yaml with a comment explaining it. The comments are still there;
the decisions now live in one place that a caller can see and a type
checker can read.
"""
from typing import Any, Dict, List

from ..base import PolicyError, PolicyPrerequisiteError
from ..spec import Policy
from ...helm import HelmRunner
from ...kube import kubectl, require_cli, require_cluster

# What the service logs when its consumer is not counting. Previously this
# checked for "rpm_consumer_start_failed_inactive", which the service has
# never logged -- the key is *connect*, not *start* (src/consumer.py:62).
# So enforcing() returned True unconditionally, including on 2026-09-15
# when the limiter had no NATS connection at all and was allowing every
# request (API-2 in bugs.md). The other two markers were missing outright.
INACTIVE_MARKERS = (
    # Backbone unreachable at connect. The service retries in the background with
    # exponential backoff (_retry_connect()) rather than giving up, so this fires once per
    # failed attempt and clears on its own once NATS comes back -- but at each log line the
    # consumer is, right then, not bound and not counting, which is exactly what this checks.
    "rpm_consumer_connect_failed_retrying",
    "rpm_consumer_durable_conflict",          # another pod holds the durable
    "rpm_consumer_bind_failed",               # transient, retried on reconnect
)


class RPMDriver:
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
        if not any((policy.limits.user_default, policy.limits.model_default,
                    policy.limits.user_overrides, policy.limits.model_overrides,
                    policy.limits.user_model_overrides)):
            warnings.append(
                "every limit is 0, which means unlimited rather than blocked "
                "— this deploys a service that denies nothing."
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

        Answering /check is not the same as limiting. The consumer is
        bound to a durable name and JetStream allows one subscription per
        durable, so a pod that lost the race — a rolling update, a second
        replica — serves happily and counts nothing.
        """
        if not policy.event_backbone_url:
            return False
        logs = kubectl(
            policy.kubeconfig_path, "logs",
            f"deploy/{policy.service_name}",
            "-n", policy.resolved_namespace, "--tail=200",
            error_cls=PolicyError, check=False,
        ) or ""
        return not any(marker in logs for marker in INACTIVE_MARKERS)
