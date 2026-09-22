"""Policy tool -- exposes multistack.Policy as an MCP tool.

Policy is a pre-request rate-limiting check the Gateway calls before
forwarding a request -- it attaches to a Gateway by configuration alone
(its `endpoint` becomes one of the Gateway's `policy_endpoints`), never
by changing the gateway image. Like Storage/MinIO/Gateway, it deploys
into an EXISTING cluster -- it needs a real kubeconfig_path."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Policy

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def admin_cp_key_error(policy: Policy) -> str | None:
    """admin_cp_url with no service key is the one combination worth
    refusing: the lookup 401s and the limiter quietly falls back to the
    static limits, so the plan limits the caller asked for never apply
    and nothing reports it. Either half alone is fine -- no URL is the
    supported pre-AD-04 behaviour, and a key with no URL is inert."""
    if policy.admin_cp_url and policy.admin_cp_service_api_key is None:
        return (
            "admin_cp_url is set but admin_cp_service_api_key is not -- the plan "
            "lookup would 401 and silently fall back to the static limits. Pass the "
            "admin control plane's SERVICE_API_KEY as admin_cp_service_api_key, or "
            "drop admin_cp_url to use the static limits on purpose"
        )
    return None


def _render_script(policy: Policy) -> str:
    """Renders a validated Policy plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one.

    Two nested models here, not one -- `options` (RPMOptions or
    TPMOptions, picked by `type`, like Storage's/Gateway's) and `limits`
    (RateLimits, always present, not Optional) -- both rendered as their
    own constructor calls rather than raw dicts."""
    fields = {k: v for k, v in dump(policy).items() if k not in ("options", "limits")}
    field_lines = [f"    {k}={v!r}," for k, v in fields.items()]

    limits_args = ", ".join(f"{k}={v!r}" for k, v in dump(policy.limits).items())
    field_lines.append(f"    limits=RateLimits({limits_args}),")

    imports = ["from multistack import Policy, PolicyBackend, RateLimits"]
    if policy.options is not None:
        # Import whichever options class this policy actually uses --
        # RPMOptions and TPMOptions are both real now (Policy supports
        # type="tpm" as of the SDK's tpm driver), and hardcoding one
        # here produced a script that referenced a class it never
        # imported, valid syntax but a NameError if actually run.
        options_cls = type(policy.options).__name__
        imports.append(f"from multistack.policy import {options_cls}")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(policy.options).items())
        field_lines.append(f"    options={options_cls}({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "policy = Policy(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = PolicyBackend()\n"
        "endpoint = backend.create(policy)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_policy_plan(policy: Policy) -> dict:
    """
    Validate a rate-limiting policy plan against the real SDK and render
    it as a single readable Python script that installs it onto an
    EXISTING cluster. Does NOT provision anything -- read-only, safe to
    call freely.

    `policy` is the real Policy type directly (requires `kubeconfig_path`
    for the cluster this installs onto, and `cache_url` -- where the
    rate counters live -- there is no safe default, an unset cache means
    no real counting). `event_backbone_url` is required too UNLESS
    `allow_no_backbone=True` is explicitly set: counting is asynchronous
    (a consumer increments counters from the gateway's events), so with
    no backbone the policy would be deployed, healthy, and silently
    enforcing nothing -- never set allow_no_backbone=True without the
    user explicitly asking for that, since it defeats the point of
    deploying this at all.

    `limits`: requests-per-minute ceilings. 0 means UNLIMITED for that
    scope, not blocked -- the inverse of what most people assume.
    Leave a scope at its 0 default rather than guessing a number the
    user didn't give.

    Once deployed, this Policy's `endpoint` is what goes into a
    Gateway's `policy_endpoints` (see build_gateway_plan) to actually
    attach it.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it.
    """
    error = admin_cp_key_error(policy)
    if error:
        return {"valid": False, "error": error}

    try:
        script = _render_script(policy)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": policy.type,
        "kubeconfig_path": policy.kubeconfig_path,
        "endpoint": policy.endpoint,
        "script": script,
    }
