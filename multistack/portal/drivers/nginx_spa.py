"""Both portals, installed from the shared `ui/chart` via the Helm layer."""
from typing import Any, Dict, List

from ...helm import HelmRunner
from ...kube import require_cli, require_cluster
from ..base import PortalError, PortalPrerequisiteError
from ..spec import Portal


class NginxSPADriver:
    def __init__(self, ready_timeout: int = 300) -> None:
        self._ready_timeout = ready_timeout

    def _helm(self, portal: Portal) -> HelmRunner:
        return HelmRunner(portal.kubeconfig_path, error_cls=PortalError)

    def _values(self, portal: Portal) -> Dict[str, Any]:
        options = portal.options
        image: Dict[str, Any] = {"repository": options.image_repository}
        if options.image_tag:
            image["tag"] = options.image_tag

        values: Dict[str, Any] = {
            "replicaCount": portal.replicas,
            "image": image,
            "service": {"port": portal.service_port},
            "config": {
                "logLevel": portal.log_level,
                "apiUpstream": portal.api_upstream,
                "apiPrefixes": list(portal.api_prefixes),
                "allowNoApi": portal.allow_no_api,
                "resolver": portal.resolver,
                "gatewayUpstream": portal.gateway_upstream,
            },
        }
        if portal.node_selector is not None:
            values["nodeSelector"] = portal.node_selector
        return values

    def check_prerequisites(self, portal: Portal) -> List[str]:
        warnings: List[str] = []
        require_cli("helm", error_cls=PortalPrerequisiteError)
        require_cluster(portal.kubeconfig_path, capability="a portal")

        if portal.allow_no_api and not portal.api_upstream:
            warnings.append(
                "allow_no_api=True with no upstream: the UI loads and every "
                "API call returns the SPA's own HTML with a 200, which the "
                "browser reports as a JSON parse error rather than an "
                "outage."
            )
        if "/api/" not in portal.api_prefixes:
            warnings.append(
                "'/api/' is not in api_prefixes: the generated client calls "
                "/api/auth/refresh directly, and without that prefix the "
                "refresh falls through to the SPA and returns HTML with a "
                "200 — the user is silently logged out."
            )
        if portal.type == "organization" and not portal.gateway_upstream:
            warnings.append(
                "organization portal with no gateway_upstream: chat calls "
                "/mg/v1/models and /mg/v1/chat/completions, which fall "
                "through to the SPA and return HTML with a 200 -- the "
                "chat page reports 'Unable to load models' with nothing "
                "in any log to explain it."
            )
        if portal.node_selector is None:
            warnings.append(
                "no node_selector: the portal image is built locally and "
                "sideloaded into one node's containerd, so a pod scheduled "
                "anywhere else stays ImagePullBackOff."
            )
        return warnings

    def create(self, portal: Portal) -> str:
        portal.validate()
        for warning in self.check_prerequisites(portal):
            print(f"[portal] warning: {warning}")

        self._helm(portal).install_or_upgrade(
            portal.options.release_name,
            chart=portal.options.chart,
            chart_version=portal.options.chart_version,
            namespace=portal.resolved_namespace,
            values=self._values(portal),
            strict_values=True,
        )
        return portal.endpoint

    def delete(self, portal: Portal) -> None:
        self._helm(portal).uninstall(
            portal.options.release_name,
            namespace=portal.resolved_namespace,
            missing_ok=True,
        )
