"""Istio: networking.istio.io/v1 Gateway + VirtualService.

No CRDs to check beyond Istio's own -- `ingress_gateway`'s
`metallb_istio` implementation already installs them as part of istiod.
Selected instead of `httproute` when a caller needs Istio traffic
features (fault injection, mirroring, subsets) that Gateway API does not
expose yet.

Same two-object, two-lifecycle split as the httproute driver: an Istio
`Gateway` is the parent, created once; a `VirtualService` is per-service.
The two `Gateway` kinds share a name only -- this one is selected by
label on the ingress Service's pods, not bound by hostname.
"""
from typing import Any, Dict, List

from ...kube import apply, kubectl, require_cli, require_cluster
from ..base import RouteError
from ..spec import Route

ISTIO_API_GROUP = "networking.istio.io"


class VirtualServiceDriver:
    def check_prerequisites(self, route: Route) -> List[str]:
        problems: List[str] = []
        try:
            require_cluster(route.kubeconfig_path, capability="route")
        except Exception as exc:  # noqa: BLE001 -- surfaced as a message, not raised
            problems.append(str(exc))
            return problems

        found = kubectl(
            route.kubeconfig_path, "get", "crd",
            f"virtualservices.{ISTIO_API_GROUP}",
            check=False, error_cls=RouteError,
        )
        if not found.strip():
            problems.append(
                "Istio's VirtualService CRD is not installed. Install the "
                "ingress_gateway capability (type='metallb_istio') first, "
                "or Istio directly, before routing a service through it."
            )
        return problems

    def _parent_manifest(self, route: Route) -> Dict[str, Any]:
        options = route.options
        return {
            "apiVersion": f"{ISTIO_API_GROUP}/v1",
            "kind": "Gateway",
            "metadata": {
                "name": route.parent_name,
                "namespace": route.parent_namespace,
            },
            "spec": {
                # Label selector, not an address: this binds to whichever
                # pods the ingress_gateway chart already labelled, the
                # same way its own install does.
                "selector": dict(options.gateway_selector),
                "servers": [{
                    "port": {
                        "number": options.listener_port,
                        "name": "http",
                        "protocol": "HTTP",
                    },
                    "hosts": list(route.hostnames) or ["*"],
                }],
            },
        }

    def _route_manifest(self, route: Route) -> Dict[str, Any]:
        return {
            "apiVersion": f"{ISTIO_API_GROUP}/v1",
            "kind": "VirtualService",
            "metadata": {"name": route.name, "namespace": route.namespace},
            "spec": {
                "hosts": list(route.hostnames) or ["*"],
                "gateways": [f"{route.parent_namespace}/{route.parent_name}"],
                "http": [{
                    "match": [{"uri": {"prefix": route.path_prefix}}],
                    "route": [{
                        "destination": {
                            "host": (
                                f"{route.service}.{route.namespace}.svc."
                                "cluster.local"
                            ),
                            "port": {"number": route.port},
                        },
                    }],
                }],
            },
        }

    def create(self, route: Route) -> str:
        require_cli("kubectl", error_cls=RouteError, purpose="applying Istio objects")

        existing = kubectl(
            route.kubeconfig_path, "get", "gateway", route.parent_name,
            "-n", route.parent_namespace, check=False, error_cls=RouteError,
        )
        if not existing.strip():
            apply(route.kubeconfig_path, self._parent_manifest(route), error_cls=RouteError)

        apply(route.kubeconfig_path, self._route_manifest(route), error_cls=RouteError)
        return route.url

    def delete(self, route: Route) -> None:
        # Same reasoning as HTTPRouteDriver.delete: the parent is shared,
        # so only the per-service object is removed here.
        kubectl(
            route.kubeconfig_path, "delete", "virtualservice", route.name,
            "-n", route.namespace, "--ignore-not-found",
            error_cls=RouteError,
        )
