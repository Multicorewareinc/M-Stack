"""Tokenizer tool -- exposes multistack.Tokenizer as an MCP tool.

Tokenizer is a small, unauthenticated HTTP service that counts tokens for
a model's encoding -- it exists so the Policy layer's `type="tpm"` driver
can answer "how many tokens was that?" for a response the upstream did
not report `usage` for (see `Policy`'s own TPMOptions.tokenizer_url,
which is the consumer this capability was built to serve). Like
Storage/MinIO/Gateway/Policy, it deploys into an EXISTING cluster -- it
needs a real kubeconfig_path."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Tokenizer

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(tokenizer: Tokenizer) -> str:
    """Renders a validated Tokenizer plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(tokenizer).items() if k != "options"]

    imports = ["from multistack import Tokenizer, TokenizerBackend"]
    if tokenizer.options is not None:
        imports.append("from multistack.tokenizer import TiktokenOptions")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(tokenizer.options).items())
        field_lines.append(f"    options=TiktokenOptions({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "tokenizer = Tokenizer(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = TokenizerBackend()\n"
        "endpoint = backend.create(tokenizer)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_tokenizer_plan(tokenizer: Tokenizer) -> dict:
    """
    Validate a Tokenizer plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `tokenizer` is the real Tokenizer type directly (requires only
    `kubeconfig_path` -- everything else has a working default). This
    service counts tokens for usage/quota accounting; it holds no
    credential and is reachable only inside the cluster, so it needs no
    secret of its own.

    Once deployed, this Tokenizer's `endpoint` is what a `type="tpm"`
    Policy's `TPMOptions.tokenizer_url` should be set to (see
    build_policy_plan) -- that wiring is NOT automatic even when both
    are built in the same conversation, since `tokenizer_url` lives on
    Policy's options object, not on Policy itself, and only the tpm
    driver reads it.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it.
    """
    try:
        script = _render_script(tokenizer)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": tokenizer.type,
        "kubeconfig_path": tokenizer.kubeconfig_path,
        "endpoint": tokenizer.endpoint,
        "script": script,
    }
