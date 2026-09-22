"""Accelerator tool -- exposes multistack.accelerator.Accelerator as an
MCP tool.

Kubernetes does not know a node has a GPU. Until something advertises
one as an extended resource, `nvidia.com/gpu` is not a thing a pod can
ask for -- so a GPU workload stays Pending, and a cluster with a healthy
card in it looks exactly like a cluster with none. This capability
installs the thing that does the advertising.

It is the one layer whose output is not an address: nothing connects to
it, and what it changes is what the scheduler believes about a node.
`Accelerator`/`AcceleratorBackend` are NOT re-exported at the top-level
`multistack` package -- import from multistack.accelerator directly,
same as IngressGateway."""

from __future__ import annotations

from pydantic import validate_call

from multistack.accelerator import Accelerator

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(accelerator: Accelerator) -> str:
    """Renders a validated Accelerator plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(accelerator).items() if k != "options"]

    imports = ["from multistack.accelerator import Accelerator, AcceleratorBackend"]
    if accelerator.options is not None:
        options_cls = type(accelerator.options).__name__
        imports.append(f"from multistack.accelerator import {options_cls}")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(accelerator.options).items())
        field_lines.append(f"    options={options_cls}({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "accelerator = Accelerator(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = AcceleratorBackend()\n"
        "backend.create(accelerator)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_accelerator_plan(accelerator: Accelerator) -> dict:
    """
    Validate an Accelerator plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `accelerator` is the real Accelerator type directly (requires only
    `kubeconfig_path` -- everything else has a working default).
    `nvidia_device_plugin` is the only implementation today; it
    advertises `nvidia.com/gpu` as a schedulable extended resource and
    does nothing else.

    `node_selector`/`tolerations` restrict which nodes the plugin runs
    on. Set them when the user says which nodes actually hold the cards
    -- a node name or label is a real fact about their cluster, never
    one to invent.

    This is what a `device="gpu"` Inference deployment needs to exist
    first (see build_inference_plan): without it, `nvidia.com/gpu` is
    not a resource any pod can request, so the model server stays
    Pending with nothing explaining why.

    Note this capability publishes nothing for other layers to consume
    -- what it changes is a node property, not an address -- so there is
    no endpoint to report or wire anywhere.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it.
    """
    try:
        script = _render_script(accelerator)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": accelerator.type,
        "kubeconfig_path": accelerator.kubeconfig_path,
        "namespace": accelerator.resolved_namespace,
        "script": script,
    }
