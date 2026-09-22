"""Observability tool -- exposes multistack.Observability as an MCP tool.

Observability is cluster-wide metrics, dashboards and alerting --
`kube_prometheus_stack` (Prometheus, Alertmanager, Grafana) today. Like
MinIO, it persists to disk, so it depends on a StorageClass existing
first -- it needs a real kubeconfig_path AND storage_class, for an
EXISTING cluster with EXISTING storage."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Observability

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(observability: Observability) -> str:
    """Renders a validated Observability plan as the single readable
    Python script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(observability).items() if k != "options"]

    imports = ["from multistack import Observability, ObservabilityBackend"]
    if observability.options is not None:
        imports.append("from multistack.observability import KubePrometheusStackOptions")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(observability.options).items())
        field_lines.append(f"    options=KubePrometheusStackOptions({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "observability = Observability(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = ObservabilityBackend()\n"
        "endpoint = backend.create(observability)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_observability_plan(observability: Observability) -> dict:
    """
    Validate an Observability plan against the real SDK and render it as
    a single readable Python script that installs it onto an EXISTING
    cluster with EXISTING storage. Does NOT provision anything --
    read-only, safe to call freely.

    `observability` is the real Observability type directly (requires
    `kubeconfig_path` for the cluster this installs onto; `storage_class`
    should be set to the real StorageClass name already on that cluster
    -- leave it None only to mean "use the cluster's default
    StorageClass", never as a way to avoid asking). Without persistence,
    every Prometheus sample and every Grafana dashboard is lost on a
    routine pod reschedule.

    `metrics_retention` must look like a Prometheus duration (e.g.
    "15d", "6h") -- a bare number is silently invalid to the chart, not
    a number of days. `prometheus_volume_size`/`grafana_volume_size`/
    `alertmanager_volume_size` must be real Kubernetes quantities with a
    unit (e.g. "20Gi") -- a bare number is bytes, not gigabytes.
    `grafana_admin_password` should normally be left unset: the driver
    generates one on first install and reads it back from the live
    Secret afterwards, so the model should never invent one.

    Once deployed, this Observability's `endpoint` (Grafana's in-cluster
    URL) is what an ingress_gateway route would front, or what another
    service reaches directly inside the cluster.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. a malformed retention or volume size).
    """
    try:
        script = _render_script(observability)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": observability.type,
        "kubeconfig_path": observability.kubeconfig_path,
        "storage_class": observability.storage_class,
        "endpoint": observability.grafana_endpoint(),
        "script": script,
    }
