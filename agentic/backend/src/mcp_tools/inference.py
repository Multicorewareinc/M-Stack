"""Inference tool -- exposes multistack.Inference as an MCP tool.

Inference is the OpenAI-compatible model server itself -- vLLM today,
serving whatever `model` the caller names. It's what a Gateway's
`upstream_url` points at. Like Storage/MinIO/Gateway/Policy, it deploys
into an EXISTING cluster -- it needs a real kubeconfig_path. See
mcp_tools/full_stack.py for composing this with other layers via Stack
(its object-store fields auto-wire from a composed MinIO layer)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import validate_call

from multistack import Inference, RKE2Node

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(inference: Inference, nodes: Optional[List[RKE2Node]]) -> str:
    """Renders a validated Inference plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.gateway._render_script: built from the validated object's
    own fields, not a hand-typed template that could silently drop one.

    `nodes` is optional (unlike Storage's, which is required) -- the
    real SDK only uses it to check prerequisites and pick a CPU dtype;
    without it, create() still works, it just skips that check."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(inference).items() if k != "options"]
    if inference.options is not None:
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(inference.options).items())
        field_lines.append(f"    options={type(inference.options).__name__}({opt_args}),")

    core_names = "Inference, InferenceBackend, RKE2Node" if nodes else "Inference, InferenceBackend"
    imports = [
        f"from multistack import {core_names}",
        "from multistack.inference import VLLMOptions",
    ]
    body = [
        "inference = Inference(",
        "\n".join(field_lines),
        ")",
    ]
    if nodes:
        node_lines = "\n".join(
            "    RKE2Node(" + ", ".join(f"{k}={v!r}" for k, v in dump(n).items()) + "),"
            for n in nodes
        )
        body += ["nodes = [\n" + node_lines + "\n]"]
        create_call = "endpoint = backend.create(inference, nodes=nodes)"
    else:
        create_call = "endpoint = backend.create(inference)"
    body += ["backend = InferenceBackend()", create_call]

    script = "\n".join(imports) + "\n\n" + "\n".join(body) + "\n"
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_inference_plan(inference: Inference, nodes: Optional[List[RKE2Node]] = None) -> dict:
    """
    Validate an Inference (model-serving) plan against the real SDK and
    render it as a single readable Python script that installs it onto
    an EXISTING cluster. Does NOT provision anything -- read-only, safe
    to call freely.

    `inference` is the real Inference type directly (requires a real
    `kubeconfig_path` for the cluster this installs onto; `model`
    defaults to a small CPU-sized model if the user doesn't name one --
    only leave it unset if they haven't specified a model AND haven't
    asked for a specific size/capability, otherwise ask). Set
    `device="gpu"` and `gpu_count>=1` for GPU serving -- never set
    `gpu_count` on a CPU device, and never invent a GPU count the user
    didn't give.

    To serve weights already in MinIO instead of pulling from a model
    registry, set `model` to an `s3://bucket/path` address, plus
    `s3_endpoint_url` and `s3_secret_name` (the Secret holding
    AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY) -- all three or none; the
    SDK rejects a partial combination.

    `nodes` is optional, unlike build_storage_plan's -- pass the target
    node list to have the real SDK check each one meets this service's
    requirements (CPU dtype support, etc.) before installing; omit it
    and that check is simply skipped.

    Once deployed, this Inference's `endpoint` is what a Gateway's
    `upstream_url` should point at (see build_gateway_plan) to actually
    serve requests through it.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it (e.g. gpu_count with device="cpu", memory_gb
    too small for kv_cache_gb+shm_gb, an s3:// model with no
    s3_endpoint_url).
    """
    try:
        script = _render_script(inference, nodes)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": inference.type,
        "kubeconfig_path": inference.kubeconfig_path,
        "model": inference.model,
        "endpoint": inference.endpoint,
        "script": script,
    }
