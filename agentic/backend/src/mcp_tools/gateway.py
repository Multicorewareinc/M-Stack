"""Gateway tool -- exposes multistack.Gateway as an MCP tool.

Gateway is the authenticated front door to inference: it proxies to an
OpenAI-compatible upstream (typically a multistack Inference deployment),
optionally checks a policy chain before each request, and optionally
publishes events. Like Storage/MinIO, it deploys into an EXISTING
cluster -- it needs a real kubeconfig_path. See mcp_tools/full_stack.py
for composing this with other layers via Stack."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Gateway

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(gateway: Gateway) -> str:
    """Renders a validated Gateway plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one.

    `options` (a nested model -- ModelGatewayOptions today) is rendered
    as its own constructor call rather than a raw dict, so the generated
    script actually constructs it rather than just showing its values."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(gateway).items() if k != "options"]
    if gateway.options is not None:
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(gateway.options).items())
        field_lines.append(f"    options={type(gateway.options).__name__}({opt_args}),")

    script = (
        "from multistack import Gateway, GatewayBackend\n"
        "from multistack.gateway import ModelGatewayOptions\n\n"
        "gateway = Gateway(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = GatewayBackend()\n"
        "endpoint = backend.create(gateway)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_gateway_plan(gateway: Gateway) -> dict:
    """
    Validate a Model Gateway plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `gateway` is the real Gateway type directly (requires
    `kubeconfig_path` for the cluster this installs onto, `upstream_url`
    for the OpenAI-compatible server it proxies to -- typically a
    an Inference deployment's endpoint -- and `api_key_secret`, the NAME of a
    Kubernetes Secret already holding the real API key, never the key
    itself). This tool is for adding a gateway to a cluster that already
    exists; if the cluster is also being built in this same request, use
    build_full_stack_plan instead.

    `org_cp_internal_url` is required too: the ORGANIZATION control
    plane's in-cluster URL, which the gateway calls to verify every /v1
    bearer token. No default, no degraded mode -- a gateway deployed
    without it passes both probes and then 503s every real request with
    "key verification is unavailable". It is never derived automatically
    (a Stack cannot tell an admin control plane from an organization
    one), so ask the user for it rather than guessing an address. The
    Secret named by api_key_secret must also carry ORG_VERIFY_API_KEY,
    matching that control plane's own MG_SERVICE_API_KEY exactly.

    Leave `policy_endpoints` empty for a plain authenticated proxy with
    no rate limiting -- only set it once a Policy has actually been
    deployed (see build_policy_plan) and its real endpoint is known.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. policy_timeout_ms too low for a
    configured policy chain, a malformed model_routes entry).
    """
    try:
        script = _render_script(gateway)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": gateway.type,
        "kubeconfig_path": gateway.kubeconfig_path,
        "upstream_url": gateway.upstream_url,
        "endpoint": gateway.endpoint,
        "script": script,
    }
