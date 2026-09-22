"""Ingress Gateway tool -- exposes multistack.ingress_gateway.IngressGateway
as an MCP tool.

Ingress Gateway is the cluster's one external front door: MetalLB hands
out a real LAN address to a LoadBalancer Service (there's no cloud
provider to do that on bare metal), and the Istio ingress gateway sits
behind that address, meant to front everything else -- the rate limiter,
the model gateway, and so on -- so those don't each need their own
LoadBalancer IP. Like Storage/MinIO/Gateway/Policy, it deploys into an
EXISTING cluster -- it needs a real kubeconfig_path. `IngressGateway` and
`IngressGatewayBackend` are NOT re-exported at the top-level `multistack`
package (only Gateway/Policy/Storage are) -- import from
multistack.ingress_gateway directly, same as this tool does."""

from __future__ import annotations

from pydantic import validate_call

from multistack.ingress_gateway import IngressGateway

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(gateway: IngressGateway) -> str:
    """Renders a validated IngressGateway plan as the single readable
    Python script that installs it -- same reasoning as
    mcp_tools.gateway._render_script: built from the validated object's
    own fields, not a hand-typed template that could silently drop one.

    `options` (MetalLBIstioOptions today) is rendered as its own
    constructor call rather than a raw dict, so the generated script
    actually constructs it rather than just showing its values."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(gateway).items() if k != "options"]
    if gateway.options is not None:
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(gateway.options).items())
        field_lines.append(f"    options={type(gateway.options).__name__}({opt_args}),")

    script = (
        "from multistack.ingress_gateway import IngressGateway, IngressGatewayBackend\n"
        "from multistack.ingress_gateway import MetalLBIstioOptions\n\n"
        "gateway = IngressGateway(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = IngressGatewayBackend()\n"
        "external_endpoint = backend.create(gateway)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_ingress_gateway_plan(gateway: IngressGateway) -> dict:
    """
    Validate an Ingress Gateway plan against the real SDK and render it
    as a single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `gateway` is the real IngressGateway type directly (requires
    `kubeconfig_path` for the cluster this installs onto, and
    `address_pool` -- the LAN IP ranges MetalLB may hand out, each either
    a "start_ip-end_ip" range or a CIDR like "192.168.1.0/24". Which
    addresses are actually free on the network is a fact only the caller
    knows -- never invent a range).

    This capability provisions the front door itself (MetalLB + the
    Istio ingress gateway) -- it does NOT wire an individual service's
    traffic through it. Routing the Model Gateway (or anything else)
    through this ingress is a separate step this tool does not cover.

    The real external address is only known once MetalLB actually
    assigns one -- unlike build_gateway_plan/build_policy_plan, this
    tool cannot return a real endpoint value, since none exists yet.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. an empty address_pool, or an entry that
    isn't a MetalLB range).
    """
    try:
        script = _render_script(gateway)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": gateway.type,
        "kubeconfig_path": gateway.kubeconfig_path,
        "address_pool": gateway.address_pool,
        "script": script,
    }
