"""MinIO tool -- exposes multistack.MinIOTenant as an MCP tool.

Like Storage, a tenant installs into an EXISTING cluster (with block
storage already on it) -- it needs a real kubeconfig_path and, usually,
a real storage_class name. This tool is for "add MinIO to a cluster
that already has storage"; see mcp_tools/full_stack.py for composing
this together with RKE2/Storage via Stack, for a stack that doesn't
exist yet."""

from __future__ import annotations

from pydantic import validate_call

from multistack import MinIOTenant

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(tenant: MinIOTenant) -> str:
    """Renders a validated MinIO tenant plan as the single readable
    Python script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one.
    MinIOTenant has no nested model fields (unlike Storage's `options`),
    so every field is a plain repr()'d value."""
    field_lines = "\n".join(f"    {k}={v!r}," for k, v in dump(tenant).items())
    script = (
        "from multistack import MinIOTenant\n"
        "from multistack.backends.minio_client import MinIOBackend\n\n"
        "tenant = MinIOTenant(\n" + field_lines + "\n)\n"
        "backend = MinIOBackend()\n"
        "info = backend.create(tenant)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_minio_plan(tenant: MinIOTenant) -> dict:
    """
    Validate a MinIO tenant plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster that already has block storage. Does NOT provision anything
    -- read-only, safe to call freely.

    `tenant` is the real MinIOTenant type directly (requires
    `kubeconfig_path` and `name`; leaving `storage_class` unset means
    "use the cluster's default StorageClass", which only exists if block
    storage was already installed and set as default). This tool is for
    adding MinIO to a cluster (and storage) that already exist; if
    either is also being built in this same request, use
    build_full_stack_plan instead, which wires the layers together
    without needing to already know values only a running cluster/storage
    layer can produce.

    Leave `root_user`/`root_password` unset to have real credentials
    generated -- never invent plausible-looking ones.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. too few drives for the requested mode,
    an unparseable volume size).
    """
    try:
        script = _render_script(tenant)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "name": tenant.name,
        "kubeconfig_path": tenant.kubeconfig_path,
        "mode": tenant.mode,
        "servers": tenant.servers,
        "volumes_per_server": tenant.volumes_per_server,
        "endpoint": tenant.endpoint(),
        "script": script,
    }
