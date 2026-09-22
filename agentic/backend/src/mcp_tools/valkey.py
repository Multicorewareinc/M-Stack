"""Valkey tool -- exposes multistack.Valkey as an MCP tool.

Valkey is a Redis-compatible cache/counter store, deployed via the
Bitnami Valkey Helm chart. It now has a real, deterministic `.endpoint`
property -- `PROVIDES = {"cache_url": "endpoint"}` -- which is what a
Policy's `cache_url` should point at. That property carries real logic
(not just a naive `f"{name}.{namespace}"` guess): Bitnami's
`common.names.fullname` collapses the release name into the chart name
when it already contains it, and prefixes it otherwise, so the Service
a caller actually reaches depends on both `name` and whatever `values`
were given, not just `name` alone. See multistack/core/valkey.py's own
`service_name` property for exactly what it accounts for.

Like Storage/MinIO/Gateway/Policy/Inference/IngressGateway, Valkey
deploys into an EXISTING cluster -- it needs a real kubeconfig_path."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Cache

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(valkey: Cache) -> str:
    """Renders a validated Valkey plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.gateway._render_script: built from the validated object's
    own fields, not a hand-typed template that could silently drop one.

    `options` (ValkeyOptions today, carrying the chart and its values)
    renders as its own constructor call rather than a raw dict, so the
    generated script constructs it -- same as
    mcp_tools.gateway._render_script. It gained an options model when
    the capability moved out of the `core/` + `backends/` split."""
    field_lines = [
        f"    {k}={v!r}," for k, v in dump(valkey).items()
        if k != "options"
    ]
    if valkey.options is not None:
        opt_args = ", ".join(
            f"{k}={v!r}" for k, v in dump(valkey.options).items())
        field_lines.append(
            f"    options={type(valkey.options).__name__}({opt_args}),")

    script = (
        "from multistack import Cache\n"
        "from multistack.cache import CacheBackend, ValkeyOptions\n\n"
        "cache = Cache(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = CacheBackend()\n"
        "release = backend.create(cache)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_valkey_plan(valkey: Cache) -> dict:
    """
    Validate a Valkey plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `valkey` is the real Valkey type directly (requires `kubeconfig_path`
    for the cluster this installs onto, and `name` to identify the Helm
    release). `namespace` and `chart` both have working defaults.

    Once deployed, this Valkey's `endpoint` is what goes into a
    Policy's `cache_url` (see build_policy_plan) to actually use it as
    the rate-limiter's counter store -- deploying Valkey alone does
    nothing on its own.

    `values` is a plain dict of raw Helm chart values, for anything this
    spec doesn't model directly -- leave it empty unless the user
    describes a specific chart setting they want. Note that `endpoint`
    depends on `values` too (Bitnami's chart collapses the release name
    into the Service name differently depending on `nameOverride`/
    `fullnameOverride` and whether Sentinel is enabled) -- always use
    the `endpoint` this tool returns rather than guessing the address
    yourself from `name` alone.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. a missing kubeconfig_path or name).
    """
    try:
        script = _render_script(valkey)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "name": valkey.name,
        "kubeconfig_path": valkey.kubeconfig_path,
        # resolved_namespace, not the raw field: the spec leaves
        # namespace None to mean "the implementation's default", and
        # a caller reading this back wants the one it will deploy to.
        "namespace": valkey.resolved_namespace,
        "endpoint": valkey.endpoint,
        "script": script,
    }
