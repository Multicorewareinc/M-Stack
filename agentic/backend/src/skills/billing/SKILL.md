---
name: billing
description: Use this skill when the user's request mentions billing, Stripe, usage metering, subscriptions, invoicing, or charging customers for usage. Covers the real MultiStack SDK Billing/BillingBackend classes.
---

# MultiStack SDK — Billing / BillingBackend

Real class signature (from `multistack`, re-exported at the top level as
`from multistack import Billing, BillingBackend`). A FastAPI service
with its own Postgres schema and migration Job — the same shape as
`ControlPlane`. Two independent surfaces in one service:

- **consumer** — drains the enricher's derived `gateway.events.enriched`
  stream into `usage`/`outbox` rows.
- **reporter** — polls `outbox`, calls Stripe's Meter Events API, and
  serves `POST /internal/v1/subscriptions` (called by
  admin-control-plane on org/plan changes) and `POST /webhooks/stripe`.

```python
class Billing(CapabilitySpec):
    type: str = "stripe"              # the only implementation
    kubeconfig_path: str              # required -- the EXISTING cluster
    namespace: Optional[str] = None   # defaults to "billing"
    options: Optional[StripeOptions] = None

    existing_secret: str              # required -- a Secret NAME, see below
    replicas: int = 1
    service_port: int = 8000
    log_level: str = "INFO"
    event_backbone_url: str = ""      # required unless allow_no_backbone
    allow_no_backbone: bool = False
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    @property
    def endpoint(self) -> str: ...    # real http://... URL, no credential
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback.
- **SECURITY — `existing_secret` is a Secret NAME, never a value.** The chart reads `DATABASE_URL`, `SERVICE_API_KEY` and the two optional Stripe secrets from a Kubernetes Secret; this spec carries only its name. Never invent one; if the user hasn't given it, ask.
- **Billing REQUIRES a real database** (`REQUIRES = ("cluster", "database")`) — it has its own Postgres schema and migration Job. Don't propose it before a CNPG/Postgres database exists (see the `cnpg` skill).
- `event_backbone_url` is required **unless** `allow_no_backbone=True` is explicitly set. The consumer drains the **enricher's** derived stream, so with no backbone nothing is metered at all. Never set that flag on your own judgement.
- **Billing counts nothing without an Enricher deployed.** Its consumer reads `gateway.events.enriched` unconditionally — unlike a `type="tpm"` policy, this isn't even configurable, so there's no override to point it elsewhere. Without an enricher publishing that stream, billing is deployed, Ready, and meters nothing. `build_full_stack_plan` rejects composing `billing` without `enricher` in the same call; this standalone tool can't check whether one already exists on the target cluster, so ask the user if they're deploying billing alone (see the `enricher` skill).
- **"Usage metering only, no Stripe integration yet" is a supported configuration.** Leaving the Stripe secrets out of the Secret leaves the reporter and the webhook route inert rather than broken — don't treat Stripe credentials as mandatory, and never ask the user to paste a Stripe key here (it belongs in the Secret).
