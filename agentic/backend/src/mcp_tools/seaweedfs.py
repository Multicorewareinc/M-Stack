"""SeaweedFS tool -- exposes multistack.SeaweedFSCluster as an MCP tool.

The platform's object storage; it replaced MinIO, whose images can no
longer be pulled without an authenticated account. It installs into an
EXISTING cluster with block storage already on it; see
mcp_tools/full_stack.py for composing it with the cluster and storage
layers when those don't exist yet."""

from __future__ import annotations

from mcp_server import mcp
from pydantic import validate_call

from mcp_tools._script import check_script, dump, dumped_lines
from multistack import SeaweedFSCluster


def _render_script(cluster: SeaweedFSCluster) -> str:
    """The single readable Python script that installs a validated
    SeaweedFS cluster, built from the object's own fields. Every field is
    a plain value, so each renders with repr()."""
    field_lines = "\n".join(dumped_lines(cluster, dump(cluster)))
    script = (
        "from multistack import SeaweedFSCluster\n"
        "from multistack.backends.seaweedfs_client import SeaweedFSBackend\n\n"
        "objectstore = SeaweedFSCluster(\n" + field_lines + "\n)\n"
        "backend = SeaweedFSBackend()\n"
        "info = backend.create(objectstore)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_seaweedfs_plan(cluster: SeaweedFSCluster) -> dict:
    """
    Validate a SeaweedFS object storage plan against the real SDK and render
    it as a single readable Python script that installs it onto an EXISTING
    cluster that already has block storage. Does NOT provision anything --
    read-only, safe to call freely.

    SeaweedFS is the platform's S3-compatible object storage (model weights,
    datasets). `cluster` is the real SeaweedFSCluster type directly (requires
    `kubeconfig_path` and `name`; leaving `storage_class` unset means "use
    the cluster's default StorageClass", which only exists if block storage
    was installed and set as default). This tool is for adding SeaweedFS to
    a cluster (and storage) that already exist; if either is being built in
    the same request, use build_full_stack_plan instead.

    Leave `access_key`/`secret_key` unset to have real credentials generated
    -- never invent plausible-looking ones.

    Returns {"valid": True, ...plan..., "script": <the deployable script>} on
    success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. an invalid replication code, a volume size
    without a unit).
    """
    try:
        script = _render_script(cluster)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "name": cluster.name,
        "kubeconfig_path": cluster.kubeconfig_path,
        "namespace": cluster.namespace,
        "volume_replicas": cluster.volume_replicas,
        "replication": cluster.replication,
        "endpoint": cluster.endpoint(),
        "script": script,
    }
