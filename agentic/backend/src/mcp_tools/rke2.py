"""RKE2 cluster tool -- exposes multistack.RKE2Cluster as an MCP tool."""

from __future__ import annotations

from pydantic import validate_call

from multistack import RKE2Cluster

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(cluster: RKE2Cluster) -> str:
    """Renders a validated cluster as the single readable Python script --
    the actual artifact handed back to whoever asked for it, not just a
    plan description.

    Built from cluster.model_dump() -- every field the cluster actually
    has, generically -- rather than a hand-typed field-by-field template.
    A hand-typed version silently drops any field it doesn't explicitly
    name (confirmed in practice: token/kubeconfig_path/ssh_key/ssh_port
    were missing from the rendered script even when set on the cluster,
    while still being part of the real, validated object). Iterating
    cluster.model_dump() means a future SDK field is automatically
    included here too -- nothing to edit when one's added, matching how
    build_rke2_cluster_plan's own `cluster: RKE2Cluster` parameter
    already avoids hand-listing fields.

    Every value is inserted with !r (repr), not hand-wrapped in quotes --
    repr() escapes whatever's actually in the string (a cluster name or
    address containing a `"` would otherwise break the generated code,
    or worse, let its content escape the string literal it was meant to
    sit inside). RKE2Cluster.validate() checks structural rules (roles,
    CNI, duplicates) but never restricts what characters a name/address
    can contain, so this has to be handled here, not assumed away."""
    cluster_lines = []
    for field, value in dump(cluster).items():
        if field == "nodes":
            node_lines = "\n".join(
                "            RKE2Node(" + ", ".join(f"{k}={v!r}" for k, v in node.items()) + "),"
                for node in value
            )
            cluster_lines.append(f"    nodes=[\n{node_lines}\n    ],")
        else:
            cluster_lines.append(f"    {field}={value!r},")

    script = (
        "from multistack import RKE2Cluster, RKE2Node\n"
        "from multistack.backends.rke2_client import RKE2Backend\n\n"
        "cluster = RKE2Cluster(\n"
        + "\n".join(cluster_lines) + "\n"
        ")\n"
        "backend = RKE2Backend()\n"
        "kubeconfig = backend.create(cluster)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_rke2_cluster_plan(cluster: RKE2Cluster) -> dict:
    """
    Validate an RKE2 cluster plan against the real SDK and render it as a
    single readable Python script. Does NOT provision anything -- read-only,
    safe to call freely.

    `cluster` is the real RKE2Cluster type directly, not individually
    re-listed fields -- the schema the model sees (and what @validate_call
    coerces a plain dict into, for direct callers) is generated straight
    from RKE2Cluster itself. Every current and future field on RKE2Cluster
    is automatically part of this tool; nothing here needs editing when
    the SDK adds one.

    A structurally invalid cluster (bad field type, no server node,
    disable_kube_proxy without cni="cilium", ...) never reaches this
    function's body at all -- RKE2Cluster validates itself the moment
    it's constructed (see its model_validator), which for this tool
    happens at the @validate_call/tool-schema boundary. Callers going
    through orchestration/graph.py's execute_tools() see that as a normal
    {"valid": False, "error": ...} result, same shape as a failure this
    function's own code detects -- see execute_tools' docstring for why
    the error has to be caught there instead of here.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success.
    """
    try:
        script = _render_script(cluster)
    except AssertionError as e:
        # Should be unreachable (see _render_script's docstring) -- if it
        # ever does happen, report it as an error rather than crash the
        # tool call or hand a human broken code.
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "cluster_name": cluster.name,
        "version": cluster.version,
        "cni": cluster.cni,
        "disable_kube_proxy": cluster.disable_kube_proxy,
        "cilium_mtu": cluster.cilium_mtu,
        "pin_node_ip": cluster.pin_node_ip,
        # cluster.nodes, not a raw argument -- model_dump() gives plain,
        # JSON-safe dicts (json.dumps() in orchestration/graph.py's
        # execute_tools can't serialize a real RKE2Node directly).
        "nodes": [n.model_dump() for n in cluster.nodes],
        "script": script,
    }
