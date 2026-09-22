"""ControlPlane tool -- exposes multistack.ControlPlane as an MCP tool.

ControlPlane is one of the platform's own API services, deployed twice
under one capability: `type="admin"` owns plans, permissions and
platform-wide administration; `type="organization"` owns an
organization's own users, keys and quotas. Both are a FastAPI service
with a Postgres schema and a migration Job, so a real database (CNPG)
and a real cache (Valkey) must already exist on the cluster -- not
auto-wired here, since credentials never travel through the SDK's
published `database_url`/`cache_url` (see the SECURITY note below).
Like Storage/MinIO/Gateway/Policy, it deploys into an EXISTING cluster
-- it needs a real kubeconfig_path.

SECURITY NOTE: `existing_secret` is a Secret *name*, never a value --
this spec does not carry DATABASE_URL/REDIS_URL/JWT_SECRET/the API keys
themselves. That Secret has to already exist on the cluster (created
separately, e.g. via `multistack.kube.apply`), holding every key
`ControlPlane(...).required_secret_keys` names for the chosen `type`.
This tool never invents a Secret name, and never asks for or renders a
credential value."""

from __future__ import annotations

from pydantic import validate_call

from multistack import ControlPlane

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(control_plane: ControlPlane) -> str:
    """Renders a validated ControlPlane plan as the single readable
    Python script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(control_plane).items() if k != "options"]

    imports = ["from multistack import ControlPlane, ControlPlaneBackend"]
    if control_plane.options is not None:
        options_cls = type(control_plane.options).__name__
        imports.append(f"from multistack.controlplane import {options_cls}")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(control_plane.options).items())
        field_lines.append(f"    options={options_cls}({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "control_plane = ControlPlane(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = ControlPlaneBackend()\n"
        "endpoint = backend.create(control_plane)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_controlplane_plan(control_plane: ControlPlane) -> dict:
    """
    Validate a ControlPlane plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `control_plane` is the real ControlPlane type directly. Requires:
    - `kubeconfig_path` for the cluster this installs onto.
    - `type`: "admin" (plans, permissions, platform-wide administration)
      or "organization" (one organization's users, keys, quotas).
    - `existing_secret`: the NAME of a Kubernetes Secret that must
      already hold DATABASE_URL, REDIS_URL, JWT_SECRET, and (for
      type="admin" only) ADMIN_API_KEY/SERVICE_API_KEY -- see this
      module's SECURITY note. Never invent a Secret name; ask the user
      for the real one if they haven't given it.

    This service is USELESS without a real database and cache already
    running -- a CNPG database and a Valkey cache must exist on the same
    cluster first (deploy them with build_cnpg_plan/build_valkey_plan,
    or build_full_stack_plan, if they don't exist yet). This tool does
    not check that for you the way build_full_stack_plan's own
    dependency check does, since a standalone call has no way to know
    what already exists on the target cluster.

    An admin and an organization control plane call each other as
    peers over the cluster network (`peer_url`, defaulting to the
    peer's own in-cluster Service name) -- deploying only one still
    validates and renders fine, but that peer link will fail at runtime
    until both exist.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. a missing existing_secret).
    """
    try:
        script = _render_script(control_plane)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": control_plane.type,
        "kubeconfig_path": control_plane.kubeconfig_path,
        "existing_secret": control_plane.existing_secret,
        "endpoint": control_plane.endpoint,
        "script": script,
    }
