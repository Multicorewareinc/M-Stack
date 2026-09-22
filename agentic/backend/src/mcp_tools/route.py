"""Route tool -- exposes multistack.Route as an MCP tool.

`build_ingress_gateway_plan` stands the front door up -- MetalLB hands a
LAN address to the Istio ingress gateway -- and deliberately stops
there. This is the capability that routes ONE service through it.

Two implementations, and the choice is not cosmetic: `httproute`
(gateway.networking.k8s.io, the Kubernetes-standard successor to
Ingress, portable across Istio/Cilium/NGINX Gateway Fabric/Envoy
Gateway) and `virtualservice` (networking.istio.io, Istio-only, for
Istio features Gateway API does not cover yet). `httproute` is the
default, for the portability reason.

Like CNPG this capability has two objects with different lifecycles --
the parent Gateway is created once, a Route is per-service and created
many times -- but unlike CNPG they are NOT two calls: `create()` applies
the parent only if absent, then the route."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Route

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(route: Route) -> str:
    """Renders a validated Route plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(route).items() if k != "options"]

    imports = ["from multistack import Route, RouteBackend"]
    if route.options is not None:
        options_cls = type(route.options).__name__
        imports.append(f"from multistack.route import {options_cls}")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(route.options).items())
        field_lines.append(f"    options={options_cls}({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "route = Route(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = RouteBackend()\n"
        "backend.create(route)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_route_plan(route: Route) -> dict:
    """
    Validate a Route plan against the real SDK and render it as a single
    readable Python script that applies it onto an EXISTING cluster that
    ALREADY has an ingress gateway. Does NOT provision anything --
    read-only, safe to call freely.

    `route` is the real Route type directly. Requires `kubeconfig_path`,
    `name` (the routing object's own name), `namespace` (which MUST be
    the namespace of the Service being routed to -- a route lives beside
    its backend), `service` (the in-cluster Service name, e.g.
    "gateway-model-gateway", not a URL), and `port`.

    `path_prefix` defaults to "/" and must start with "/" -- a relative
    path matches nothing and fails silently at the gateway rather than
    at admission. `hostnames` are bare hosts, never URLs: no scheme, no
    port, no path.

    `type` picks the implementation: "httproute" (the default, portable
    Gateway API) or "virtualservice" (Istio-only). Don't switch to
    virtualservice unless the user asks for Istio-specific traffic
    management.

    This REQUIRES an ingress gateway to already exist (see
    build_ingress_gateway_plan) -- it routes through the front door, it
    does not create one.

    The route's real external URL is only known once MetalLB has
    assigned the ingress an address, so `route_url` here may contain a
    `<ingress-address>` placeholder rather than a real host. Never
    present that as a real address.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. a relative path_prefix, a hostname that
    is really a URL, an empty namespace).
    """
    # Route has no model_validator re-running its own validate() at
    # construction (the same narrow gap Portal has), so the namespace and
    # service rules only fire when something calls validate() explicitly.
    try:
        route.validate()
    except ValueError as e:
        return {"valid": False, "error": str(e)}

    try:
        script = _render_script(route)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": route.type,
        "kubeconfig_path": route.kubeconfig_path,
        "service": route.service,
        "route_url": route.url,
        "script": script,
    }
