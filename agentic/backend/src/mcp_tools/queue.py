"""Queue tool -- exposes multistack.Queue (NATS JetStream) as an MCP tool.

The queue is the platform's event backbone. `QueueBackend.create()`
installs NATS and creates both of the platform's streams:
`gateway.events` (raw), which the gateway publishes and the RPM limiter
and enricher consume, and `gateway.events.enriched`, which the enricher
republishes and the TPM limiter and billing consume. Persistent, so it
needs a StorageClass on an EXISTING cluster."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Queue

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(queue: Queue) -> str:
    """Renders a validated Queue plan as the single readable Python script
    that installs it, built from the validated object's own fields."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(queue).items() if k != "options"]

    imports = ["from multistack import Queue, QueueBackend"]
    if queue.options is not None:
        imports.append("from multistack import NatsQueueOptions")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(queue.options).items())
        field_lines.append(f"    options=NatsQueueOptions({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "queue = Queue(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = QueueBackend()\n"
        "backend.create(queue)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_queue_plan(queue: Queue) -> dict:
    """
    Validate a Queue (NATS JetStream event backbone) plan against the real
    SDK and render it as a single readable Python script that installs it
    onto an EXISTING cluster with EXISTING storage. Does NOT provision
    anything -- read-only, safe to call freely.

    `queue` is the real Queue type directly (requires `kubeconfig_path`
    for the cluster this installs onto; `storage_class` should be the real
    StorageClass on that cluster -- JetStream persists its streams there,
    and without it every retained event is lost on a pod reschedule).
    Creating it also creates both platform streams, gateway.events and
    gateway.events.enriched.

    Only for adding the backbone to a cluster whose other components the
    user has said already exist. For a new deployment of anything that
    uses events (rate limiting, the enricher, billing), use
    build_full_stack_plan with `queue`, which wires this backbone into
    every consumer.

    Once deployed, this Queue's `endpoint` is the `event_backbone_url`
    the gateway, rate limiters, enricher and billing all point at.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it.
    """
    try:
        script = _render_script(queue)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": queue.type,
        "kubeconfig_path": queue.kubeconfig_path,
        "storage_class": queue.storage_class,
        "endpoint": queue.endpoint,
        "script": script,
    }
