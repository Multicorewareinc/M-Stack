"""The first-party gateway, installed from its chart via the Helm layer."""
import json
from typing import Any, Dict, List

from ..base import GatewayError, GatewayPrerequisiteError
from ..spec import REQUIRED_SECRET_KEYS, Gateway
from ...helm import HelmRunner
from ...kube import kubectl, require_cli, require_cluster


class ModelGatewayDriver:
    def __init__(self, ready_timeout: int = 300) -> None:
        self._ready_timeout = ready_timeout

    def _helm(self, gateway: Gateway) -> HelmRunner:
        return HelmRunner(gateway.kubeconfig_path, error_cls=GatewayError)

    def _values(self, gateway: Gateway) -> Dict[str, Any]:
        options = gateway.options
        values: Dict[str, Any] = {
            "replicaCount": gateway.replicas,
            "service": {"port": gateway.service_port},
            "config": {
                "upstreamUrl": gateway.upstream_url,
                "orgCpInternalUrl": gateway.org_cp_internal_url,
                "requestTimeout": gateway.request_timeout,
                # The service reads MODEL_ROUTES as a JSON string.
                "modelRoutes": json.dumps(gateway.model_routes),
                "policy": {
                    "endpoints": list(gateway.policy_endpoints),
                    "timeoutMs": gateway.policy_timeout_ms,
                    "failMode": gateway.policy_fail_mode,
                },
                "events": {
                    "backboneUrl": gateway.event_backbone_url,
                    "streamName": options.event_stream_name,
                    "streamSubject": options.event_stream_subject,
                    "captureBodies": options.capture_bodies,
                    "maxInflight": options.max_inflight,
                },
            },
            # By name only. The chart refuses to install without a key
            # source, because the image ships a development default.
            "secret": {"existingSecret": gateway.api_key_secret},
        }
        if gateway.network_policy_allowed_ingress:
            values["networkPolicy"] = {
                "enabled": True,
                "allowedIngress": gateway.network_policy_allowed_ingress,
            }
        if options.image_tag:
            values["image"] = {"tag": options.image_tag}
        # Absent leaves the chart's own default in place; an explicit {}
        # overrides it with "schedule anywhere", which is a different
        # statement and sometimes the intended one.
        if gateway.node_selector is not None:
            values["nodeSelector"] = gateway.node_selector
        if gateway.tolerations is not None:
            values["tolerations"] = gateway.tolerations
        return values

    def check_prerequisites(self, gateway: Gateway) -> List[str]:
        warnings: List[str] = []
        require_cli("helm", error_cls=GatewayPrerequisiteError)
        require_cluster(gateway.kubeconfig_path, capability="a gateway")

        # The Secret is referenced, not created, so its contents are a
        # cluster fact this can check rather than a spec error. Existence
        # alone is not enough: a Secret missing ORG_VERIFY_API_KEY installs
        # cleanly and passes both probes, and only the first real request
        # reveals it, as a 503 from verify_api_key rather than anything
        # that names this Secret.
        raw = kubectl(
            gateway.kubeconfig_path, "get", "secret", gateway.api_key_secret,
            "-n", gateway.resolved_namespace, "-o", "json",
            error_cls=GatewayError, check=False,
        )
        if not (raw or "").strip():
            raise GatewayPrerequisiteError(
                f"Secret '{gateway.api_key_secret}' does not exist in "
                f"namespace '{gateway.resolved_namespace}'. It must hold "
                f"{', '.join(REQUIRED_SECRET_KEYS)}, and may also hold "
                "UPSTREAM_API_KEY and a MODEL_ROUTES carrying provider "
                "credentials. Create it with multistack.kube.apply, which "
                "pipes the manifest to stdin — `kubectl create secret "
                "--from-literal` puts the key in the process arguments, "
                "where any local user can read it."
            )

        try:
            present = set(json.loads(raw).get("data", {}))
        except json.JSONDecodeError:
            present = set()

        missing = [k for k in REQUIRED_SECRET_KEYS if k not in present]
        if missing:
            raise GatewayPrerequisiteError(
                f"Secret '{gateway.api_key_secret}' is missing "
                f"{', '.join(missing)}. The chart installs and the pod goes "
                "Ready regardless — a missing ORG_VERIFY_API_KEY makes every "
                "/v1 request 503 with 'key verification is unavailable', "
                "because verify_api_key has no fallback once it needs to "
                "call the organization control plane."
            )

        if not gateway.policy_endpoints:
            warnings.append(
                "no policy endpoints, so the chain is inert: every "
                "authenticated request is forwarded with no rate limiting "
                "or quota check."
            )
        if not gateway.event_backbone_url:
            warnings.append(
                "no event backbone, so no usage events are published and "
                "nothing downstream can bill, audit or count tokens."
            )
        if gateway.options.capture_bodies:
            warnings.append(
                "capture_bodies=True: published events carry full prompt and "
                "response text. That is a governance decision — check it is "
                "one you meant to make."
            )
        return warnings

    def create(self, gateway: Gateway) -> str:
        self._helm(gateway).install_or_upgrade(
            gateway.options.release_name,
            chart=gateway.options.chart,
            chart_version=gateway.options.chart_version,
            namespace=gateway.resolved_namespace,
            values=self._values(gateway),
            strict_values=True,
        )
        return gateway.endpoint

    def delete(self, gateway: Gateway) -> None:
        self._helm(gateway).uninstall(
            gateway.options.release_name,
            namespace=gateway.resolved_namespace,
            missing_ok=True,
        )
