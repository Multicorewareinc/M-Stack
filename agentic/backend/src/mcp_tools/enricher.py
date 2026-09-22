"""Enricher tool -- exposes multistack.Enricher as an MCP tool.

The Enricher guarantees a token count on every response event. It
consumes the gateway's raw `gateway.events` stream, falls back to the
Tokenizer when a response carries no `usage` block, and republishes to a
derived stream (`gateway.events.enriched`) whose retention and cursor
stay independent of the raw input.

This is the capability that makes ADR-030 true: `rate-limiter-tpm` and
`billing` both read that derived stream instead of calling a tokenizer
themselves. Without an Enricher deployed and consuming, neither fails --
they go quietly uncounted, which is the worse outcome. Like
Storage/MinIO/Gateway/Policy it deploys into an EXISTING cluster."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Enricher

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(enricher: Enricher) -> str:
    """Renders a validated Enricher plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(enricher).items() if k != "options"]

    imports = ["from multistack import Enricher, EnricherBackend"]
    if enricher.options is not None:
        imports.append("from multistack.enricher import EnricherOptions")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(enricher.options).items())
        field_lines.append(f"    options=EnricherOptions({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "enricher = Enricher(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = EnricherBackend()\n"
        "backend.create(enricher)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_enricher_plan(enricher: Enricher) -> dict:
    """
    Validate an Enricher plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `enricher` is the real Enricher type directly (requires
    `kubeconfig_path` for the cluster this installs onto).

    `event_backbone_url` is required UNLESS `allow_no_backbone=True` is
    explicitly set. The enricher exists to consume one stream and
    publish another, so with no backbone it is deployed, healthy, and
    doing nothing -- and the things downstream of it (a `type="tpm"`
    rate limiter, and billing) then count nothing at all rather than
    erroring. Never set allow_no_backbone=True on your own judgement;
    only when the user explicitly asks to deploy ahead of the backbone.

    `options.tokenizer_url` is where the Tokenizer's endpoint goes (see
    build_tokenizer_plan) -- that is the fallback used when a response
    event carries no `usage` block, which is mainly streaming
    responses. The Enricher, not the rate limiter, is the tokenizer's
    consumer as of ADR-030.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it.
    """
    try:
        script = _render_script(enricher)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": enricher.type,
        "kubeconfig_path": enricher.kubeconfig_path,
        "enriched_subject": enricher.options.enriched_subject if enricher.options else None,
        "script": script,
    }
