"""Storage tool -- exposes multistack.Storage as an MCP tool.

Storage installs block storage (a StorageClass) onto an EXISTING
cluster -- it needs a real kubeconfig_path and a node list to check
prerequisites against, unlike RKE2Cluster which creates the cluster
those nodes belong to. This tool is for "add storage to a cluster I
already have"; see mcp_tools/full_stack.py for composing this together
with RKE2/MinIO via Stack, for a cluster that doesn't exist yet."""

from __future__ import annotations

from typing import List

from pydantic import validate_call

from multistack import RKE2Node, Storage

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(storage: Storage, nodes: List[RKE2Node]) -> str:
    """Renders a validated Storage plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one.

    `options` (a nested model -- LonghornOptions today) is rendered as
    its own constructor call rather than a raw dict, so the generated
    script actually constructs it rather than just showing its values."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(storage).items() if k != "options"]
    if storage.options is not None:
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(storage.options).items())
        field_lines.append(f"    options={type(storage.options).__name__}({opt_args}),")

    node_lines = "\n".join(
        "    RKE2Node(" + ", ".join(f"{k}={v!r}" for k, v in dump(n).items()) + "),"
        for n in nodes
    )

    script = (
        "from multistack import RKE2Node, Storage, StorageBackend\n"
        "from multistack.storage import LonghornOptions\n\n"
        "storage = Storage(\n" + "\n".join(field_lines) + "\n)\n"
        "nodes = [\n" + node_lines + "\n]\n"
        "backend = StorageBackend()\n"
        "backend.install_prerequisites(storage, nodes)\n"
        "storage_class = backend.create(storage, nodes=nodes)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_storage_plan(storage: Storage, nodes: List[RKE2Node]) -> dict:
    """
    Validate a block-storage plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `storage` is the real Storage type directly (requires `type` --
    "longhorn" is the only supported value today -- and a real
    `kubeconfig_path` for the cluster this installs onto). This tool is
    for adding storage to a cluster that already exists; if the cluster
    itself is also being built in this same request, use
    build_full_stack_plan instead, which wires the two together without
    needing to already know the new cluster's kubeconfig path.

    `nodes` is the target cluster's node list -- Storage itself doesn't
    carry node addresses (an already-running cluster does), but the SDK
    still needs them here to check each one meets the storage
    implementation's requirements before installing.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. an unsupported type, a bad
    kubeconfig_path).
    """
    try:
        script = _render_script(storage, nodes)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": storage.type,
        "kubeconfig_path": storage.kubeconfig_path,
        "replica_count": storage.replica_count,
        "storage_class_name": storage.storage_class_name,
        "script": script,
    }
