"""Gateway API: gateway.networking.k8s.io/v1 Gateway + HTTPRoute.

Two objects, one lifecycle each, kept apart the way CNPG's operator and
cluster are:

  parent Gateway   created once, binds to the ingress_gateway's own
                   Service by hostname rather than provisioning a second
                   Service and taking a second address from what is
                   usually a one- or two-address MetalLB pool.
  HTTPRoute        created per service, attaches to the parent by name.

`create()` applies both, idempotently -- a second call with the same
parent name is a no-op for the parent and an update for the route.
"""
from typing import Any, Dict, List

from ...kube import apply, kubectl, require_cli, require_cluster
from ..base import RouteError, RoutePrerequisiteError
from ..spec import Route

GATEWAY_API_GROUP = "gateway.networking.k8s.io"
GATEWAY_CRD = f"gateways.{GATEWAY_API_GROUP}"


class HTTPRouteDriver:
    def check_prerequisites(self, route: Route) -> List[str]:
        problems: List[str] = []
        try:
            require_cluster(route.kubeconfig_path, capability="route")
        except Exception as exc:  # noqa: BLE001 -- surfaced as a message, not raised
            problems.append(str(exc))
            return problems

        # The CRDs are cluster-scoped and installed by neither RKE2 nor
        # any chart this SDK deploys -- checked explicitly so a missing
        # CRD fails with "install Gateway API" rather than a raw
        # kubectl "no matches for kind" three lines deep in a driver.
        found = kubectl(
            route.kubeconfig_path, "get", "crd", GATEWAY_CRD,
            check=False, error_cls=RouteError,
        )
        if not found.strip():
            problems.append(
                "Gateway API CRDs are not installed. Apply "
                "https://github.com/kubernetes-sigs/gateway-api/releases/"
                "download/v1.2.1/standard-install.yaml first -- neither "
                "RKE2 nor any chart this SDK deploys installs them."
            )
        return problems

    def _parent_manifest(self, route: Route) -> Dict[str, Any]:
        options = route.options
        return {
            "apiVersion": f"{GATEWAY_API_GROUP}/v1",
            "kind": "Gateway",
            "metadata": {
                "name": route.parent_name,
                "namespace": route.parent_namespace,
            },
            "spec": {
                "gatewayClassName": options.gateway_class_name,
                # A hostname, not an IP: the ingress_gateway Service is
                # already a LoadBalancer with its own MetalLB address, so
                # this binds to it rather than asking the GatewayClass to
                # provision -- and take -- a second one.
                "addresses": [{
                    "type": "Hostname",
                    "value": (
                        f"{route.ingress_service}.{route.parent_namespace}"
                        ".svc.cluster.local"
                    ),
                }],
                "listeners": [{
                    "name": "http",
                    "protocol": "HTTP",
                    "port": options.listener_port,
                    "allowedRoutes": {"namespaces": {"from": "All"}},
                }],
            },
        }

    def _route_manifest(self, route: Route) -> Dict[str, Any]:
        options = route.options
        rule: Dict[str, Any] = {
            "matches": [{
                "path": {"type": options.path_match_type, "value": route.path_prefix},
            }],
            "backendRefs": [{"name": route.service, "port": route.port}],
        }
        manifest: Dict[str, Any] = {
            "apiVersion": f"{GATEWAY_API_GROUP}/v1",
            "kind": "HTTPRoute",
            "metadata": {"name": route.name, "namespace": route.namespace},
            "spec": {
                "parentRefs": [{
                    "name": route.parent_name,
                    "namespace": route.parent_namespace,
                }],
                "rules": [rule],
            },
        }
        if route.hostnames:
            manifest["spec"]["hostnames"] = list(route.hostnames)
        return manifest

    def create(self, route: Route) -> str:
        require_cli("kubectl", error_cls=RouteError, purpose="applying Gateway API objects")

        # Cheap existence check rather than a get-then-apply merge: the
        # parent's own fields never depend on any one route, so applying
        # it again is exactly as correct as applying it once and costs
        # one extra kubectl call only the first time a service routes.
        existing = kubectl(
            route.kubeconfig_path, "get", "gateway", route.parent_name,
            "-n", route.parent_namespace, check=False, error_cls=RouteError,
        )
        if not existing.strip():
            apply(route.kubeconfig_path, self._parent_manifest(route), error_cls=RouteError)

        apply(route.kubeconfig_path, self._route_manifest(route), error_cls=RouteError)
        return route.url

    def delete(self, route: Route) -> None:
        # The parent is never deleted here -- it is shared by every route
        # bound to it, and this driver has no way to know whether another
        # one still needs it. Removing the front door itself is a
        # deliberate, separate action, not a side effect of one service's
        # teardown.
        kubectl(
            route.kubeconfig_path, "delete", "httproute", route.name,
            "-n", route.namespace, "--ignore-not-found",
            error_cls=RouteError,
        )
