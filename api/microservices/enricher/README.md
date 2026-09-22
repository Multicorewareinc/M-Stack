# Event Enricher

A downstream stage of the gateway event backbone (ADR-030). It consumes `response`
events from the gateway's canonical `gateway.events` stream, **guarantees a token
count** on each, and republishes them to a separate `gateway.events.enriched` stream
that `rate-limiter-tpm` and `billing` consume. The gateway is unchanged; the enricher
is one more downstream consumer (ADR-006/ADR-007).

## Why it exists

Two consumers need a reliable per-response token count (TPM and billing). Rather than
each re-implement the tokenizer fallback — and risk disagreeing on a count — tokenization
is centralized here, once (ADR-030 AD-02).

## What it does

For each `response` event:

1. **provider** — a numeric `usage.total_tokens` is present → keep it, no tokenizer call.
   Any provider `prompt_tokens`/`completion_tokens` are preserved additively so billing can
   price input vs output (ADR-030 D6).
2. **estimated** — no numeric total but captured body text + a configured tokenizer → call
   the shared `tokenizer` `POST /tokenize` on the completion text. Undercounts prompt tokens
   by design (ADR-011 AD-06 — needs the gateway's `EVENT_CAPTURE_BODIES=true`).
3. **none** — neither a count nor usable text → republished with `total_tokens: null` and a
   `reason` (`no_body` | `tokenizer_unset` | `tokenizer_error`). Never fabricated.

The enriched event carries `type`, `request_id`, `principal`, `model`, `status`, `ts`,
`duration_ms`, `stream`, and the normalized `usage`; the original `body` is dropped.

## Contracts

- **Consumes**: `gateway.events` (stream `GATEWAY_EVENTS`), `response` events only.
- **Produces**: `gateway.events.enriched` (stream `GATEWAY_EVENTS_ENRICHED`), `Nats-Msg-Id
  = request_id` for dedup. `usage = {total_tokens: int|null, source: provider|estimated|none
  [, reason][, prompt_tokens, completion_tokens]}`.
- **Calls**: `tokenizer` `POST /tokenize {text} -> {tokens, tokenizer}` (ADR-014).

## Reliability

- Durable JetStream consumer, `DeliverPolicy.NEW`, resilient (ADR-016): a down backbone
  never crashes the service and the subscription rebinds on reconnect.
- **Ack-after-republish** (ADR-030 D5): unlike TPM (which acks always, fail-open soft
  counts), a dropped republish here is real data loss, so the enricher acks only after a
  successful republish; a failing republish is **not** acked and JetStream redelivers,
  bounded by `MAX_DELIVER` + `ACK_WAIT_SECONDS`. Terminal exhaustion is logged (naming the
  `request_id`) and metered — never silent. Recovery is operator replay from the retained
  input stream (size `GATEWAY_EVENTS` retention accordingly).
- Idempotent: `Nats-Msg-Id = request_id` dedups within the enriched stream's window;
  downstream consumers stay idempotent on `request_id` outside it (ADR-008).

## Config

See `.env.example`. Notably: empty `EVENT_BACKBONE_URL` ⇒ inert; empty `TOKENIZER_URL` ⇒
degraded-not-dead (`source="none"`, `reason="tokenizer_unset"`); `ACK_WAIT_SECONDS` must be
≥ `TOKENIZER_TIMEOUT_MS` + a republish margin.

## Ops

- `GET /health` — liveness. `GET /metrics` — Prometheus. The `enricher_consumer_active`
  gauge is the readiness signal (no `/ready`).
- Metrics: `enricher_events_total{source,reason}`, `enricher_republish_failures_total{terminal}`,
  `enricher_tokenizer_fallback_total{outcome}`, `enricher_consumer_active`.

## Tests

`api/tests/enricher/` — offline (`pytest`) with the tokenizer stubbed via
`httpx.MockTransport` and a fake JetStream publisher (no live NATS), plus the Docker `test`
stage. See ADR-030.
