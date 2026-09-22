"""Billing tool -- exposes multistack.Billing as an MCP tool.

Billing is usage metering and Stripe subscriptions: a FastAPI service
with its own Postgres schema and migration Job, the same shape as
ControlPlane. Two independent surfaces in one service -- a consumer that
drains the enricher's derived `gateway.events.enriched` stream into
usage/outbox rows, and a reporter that polls those rows and calls
Stripe's Meter Events API.

SECURITY NOTE: `existing_secret` is a Secret *name*, never a value --
this spec does not carry DATABASE_URL, SERVICE_API_KEY, or either Stripe
secret. That Secret has to already exist on the cluster, created
separately. This tool never invents a Secret name and never asks for or
renders a credential value."""

from __future__ import annotations

from pydantic import validate_call

from multistack import Billing

from mcp_server import mcp
from mcp_tools._script import check_script, dump


def _render_script(billing: Billing) -> str:
    """Renders a validated Billing plan as the single readable Python
    script that installs it -- same reasoning as
    mcp_tools.rke2._render_script: built from the validated object's own
    fields, not a hand-typed template that could silently drop one."""
    field_lines = [f"    {k}={v!r}," for k, v in dump(billing).items() if k != "options"]

    imports = ["from multistack import Billing, BillingBackend"]
    if billing.options is not None:
        options_cls = type(billing.options).__name__
        imports.append(f"from multistack.billing import {options_cls}")
        opt_args = ", ".join(f"{k}={v!r}" for k, v in dump(billing.options).items())
        field_lines.append(f"    options={options_cls}({opt_args}),")

    script = (
        "\n".join(imports) + "\n\n"
        "billing = Billing(\n" + "\n".join(field_lines) + "\n)\n"
        "backend = BillingBackend()\n"
        "endpoint = backend.create(billing)\n"
    )
    check_script(script)
    return script


@mcp.tool()
@validate_call
def build_billing_plan(billing: Billing) -> dict:
    """
    Validate a Billing plan against the real SDK and render it as a
    single readable Python script that installs it onto an EXISTING
    cluster. Does NOT provision anything -- read-only, safe to call
    freely.

    `billing` is the real Billing type directly. Requires:
    - `kubeconfig_path` for the cluster this installs onto.
    - `existing_secret`: the NAME of a Kubernetes Secret that must
      already hold DATABASE_URL and SERVICE_API_KEY (and, only if Stripe
      reporting is wanted, the Stripe secret key and webhook secret).
      See this module's SECURITY note -- never invent a Secret name; ask
      the user for the real one if they haven't given it.

    Billing REQUIRES a real database (CNPG/Postgres) already on the
    cluster -- it has its own schema and migration Job. It also needs
    `event_backbone_url` unless `allow_no_backbone=True` is explicitly
    set: the consumer drains the ENRICHER's derived
    `gateway.events.enriched` stream, so with no backbone nothing is
    metered at all. Never set allow_no_backbone=True on your own
    judgement.

    "Usage metering only, no Stripe integration yet" is a genuinely
    supported configuration -- leaving the Stripe secrets out of the
    Secret leaves the reporter and the webhook route inert rather than
    broken, so don't treat Stripe credentials as mandatory.

    Returns {"valid": True, ...plan..., "script": <the deployable script>}
    on success, or {"valid": False, "error": <reason>} if the SDK's own
    validation rejected it.
    """
    try:
        script = _render_script(billing)
    except AssertionError as e:
        return {"valid": False, "error": str(e)}

    return {
        "valid": True,
        "type": billing.type,
        "kubeconfig_path": billing.kubeconfig_path,
        "existing_secret": billing.existing_secret,
        "endpoint": billing.endpoint,
        "script": script,
    }
