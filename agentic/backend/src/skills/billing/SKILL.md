---
name: billing
description: Use this skill when the user's request mentions billing, Stripe, metering, subscriptions, an invoice, invoicing, or charging customers. Covers the real MultiStack SDK Billing/BillingBackend classes.
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
class StripeOptions(BaseModel):
    release_name: str = "billing"
    chart: str = "api/microservices/billing/chart"
    chart_version: Optional[str] = None
    image_tag: Optional[str] = None
    image_repository: Optional[str] = None   # full image path; None = the chart's own registry. Set it only when the user names a registry or mirror
    durable_name: str = "billing-metering"   # JetStream durable consumer name
    stripe_meter_event_name: str = "tokens_used"             # the Stripe Meter Event name usage is reported under
    stripe_meter_customer_field: str = "stripe_customer_id"  # payload key the principal is sent under (an internal org_id until the org->customer mapping lands)
    reporter_poll_interval_seconds: int = 5      # > 0; how often the reporter polls the outbox
    reporter_batch_size: int = 50                # >= 1; outbox rows sent per poll
    reporter_max_attempts: int = 5               # >= 1; tries before a report is given up on
    reporter_backoff_base_seconds: int = 30      # > 0; must not exceed reporter_backoff_max_seconds
    reporter_backoff_max_seconds: int = 3600     # > 0
    migration_deadline_seconds: int = 120        # > 0; the migration Job fails the install rather than hanging it
    migration_backoff_limit: int = 2             # >= 0

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
    affinity: Optional[Dict[str, Any]] = None
    node_selector: Optional[Dict[str, str]] = None
    tolerations: Optional[List[Dict[str, Any]]] = None

    @property
    def endpoint(self) -> str: ...    # real http://... URL, no credential
```

Rules when generating code:
- `kubeconfig_path` is required and has no ambient fallback.
- `affinity`/`node_selector`/`tolerations` pin this service to a particular node — there is no in-cluster registry, so a service image exists only on the node it was imported to, and a pod scheduled elsewhere sits in `ImagePullBackOff`. Leave unset unless the user names the node — unset keeps the chart's own default (off the control plane); an explicit `{}` means "schedule anywhere", the only way a single-node cluster can actually schedule these pods.
- **SECURITY — `existing_secret` is a Secret NAME, never a value.** The chart reads `DATABASE_URL`, `SERVICE_API_KEY` and the two optional Stripe secrets from a Kubernetes Secret; this spec carries only its name. Never invent one; if the user hasn't given it, ask.
- **Billing REQUIRES a real database** (`REQUIRES = ("cluster", "database")`) — it has its own Postgres schema and migration Job. Don't propose it before a CNPG/Postgres database exists (see the `cnpg` skill).
- `event_backbone_url` is required **unless** `allow_no_backbone=True` is explicitly set. The consumer drains the **enricher's** derived stream, so with no backbone nothing is metered at all. Never set that flag on your own judgement.
- **Billing counts nothing without an Enricher deployed.** Its consumer reads `gateway.events.enriched` unconditionally — unlike a `type="tpm"` policy, this isn't even configurable, so there's no override to point it elsewhere. Without an enricher publishing that stream, billing is deployed, Ready, and meters nothing. `build_full_stack_plan` rejects composing `billing` without `enricher` in the same call; this standalone tool can't check whether one already exists on the target cluster, so ask the user if they're deploying billing alone (see the `enricher` skill).
- **"Usage metering only, no Stripe integration yet" is a supported configuration.** Leaving the Stripe secrets out of the Secret leaves the reporter and the webhook route inert rather than broken — don't treat Stripe credentials as mandatory, and never ask the user to paste a Stripe key here (it belongs in the Secret).
