"""Portal tool -- exposes multistack.Portal as an MCP tool.

Portal is one of the platform's single-page web UIs, deployed twice
under one capability: `type="admin"` (the admin UI) and
`type="organization"` (the organization UI). Both are the same chart --
static files served by nginx, which also reverse-proxies API requests so
the browser makes same-origin requests -- deployed twice with a
different image and a different upstream. Like Storage/MinIO/Gateway/
Policy, it deploys into an EXISTING cluster -- it needs a real
kubeconfig_path."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Portal

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(portal: Portal) -> str:
    """Renders a validated Portal plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(portal).items() if k != "options"]

    imports = ["from multistack import Portal, PortalBackend"]
    if portal.options is not None:
        options_cls = type(portal.options).__name__
        imports.append(f"from multistack.portal import {options_cls}")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(portal.options).items())
        field_lines.append(f"    options={options_cls}({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "portal = Portal(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = PortalBackend()\n"
        "endpoint = backend.create(portal)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_portal_plan(portal: Portal) -> dict:
    """
    Validate a Portal plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `portal` is the real Portal type directly (requires `kubeconfig_path`
    for the cluster this installs onto; `type`: "admin" or
    "organization" -- these are different images, not a naming choice).

    `api_upstream` is the in-cluster URL nginx reverse-proxies API
    requests to -- normally the matching ControlPlane's `endpoint` (see
    build_controlplane_plan). It's required UNLESS `allow_no_api=True` is
    explicitly set: without a real upstream, every API call falls
    through to the single-page app and returns 200 with the index.html
    body, so the browser fails on a JSON parse error with nothing in any
    log to explain it -- never set allow_no_api=True without the user
    explicitly asking to deploy the UI ahead of its API.

    Once deployed, this Portal's `endpoint` is where an ingress_gateway
    route (or another client) reaches it.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. api_upstream left empty without
    allow_no_api).
    """
    # Unlike Gateway/Policy, Portal's own extra rule (api_upstream vs.
    # allow_no_api) is NOT re-run automatically at construction -- Portal
    # has no model_validator override calling its own validate() the way
    # Policy._validate_on_construction does, so a Portal with an empty
    # api_upstream and allow_no_api=False constructs successfully and
    # only fails once a driver actually calls validate() at deploy time.
    # Same gap category as CNPG's validate_cluster() -- call it explicitly
    # here rather than rendering a script that looks valid but isn't.
    try:
        portal.validate()
    except ValueError as e:
        return {"valid": False, "error": str(e)}

    try:
        script = _render_script(portal)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": portal.type,
        "kubeconfig_path": portal.kubeconfig_path,
        "api_upstream": portal.api_upstream,
        "endpoint": portal.endpoint,
        "script": script,
    }
