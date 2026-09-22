"""Service configuration. Single Settings class, read from env (and optional .env).

The enricher is a downstream stage of the gateway event backbone (ADR-030): it
consumes `response` events from GATEWAY_EVENTS/gateway.events, guarantees a token
count, and republishes to a separate GATEWAY_EVENTS_ENRICHED/gateway.events.enriched.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"

    # Backbone connection. Empty EVENT_BACKBONE_URL => no consumer runs => the enricher
    # is inert (nothing to consume/republish). Same discipline as the other consumers.
    event_backbone_url: str = ""  # e.g. "nats://nats:4222"; empty = no consumer

    # Input: the gateway's canonical stream (unchanged, ADR-007). The enricher consumes
    # its `response` events; it never modifies the gateway or this stream's contract.
    event_stream_name: str = "GATEWAY_EVENTS"
    event_stream_subject: str = "gateway.events"
    event_durable_name: str = "enricher"  # durable JetStream consumer name

    # Output: a SEPARATE derived stream (ADR-030 D1) so retention/cursors are independent
    # and enriched output never feeds back into the input subject.
    enriched_stream_name: str = "GATEWAY_EVENTS_ENRICHED"
    enriched_subject: str = "gateway.events.enriched"

    # Bounded redelivery for the not-ack-on-republish-failure path (ADR-030 D5). A failing
    # republish is retried up to max_deliver times (AckWait between attempts); terminal
    # exhaustion is logged + metered, never silently dropped. ack_wait_seconds MUST be >=
    # tokenizer_timeout_ms + a republish margin, else JetStream redelivers mid-flight.
    max_deliver: int = 5
    ack_wait_seconds: int = 30

    # Tokenizer fallback (ADR-030 AD-02/AD-03), used only when a response event's `usage`
    # is absent. Empty TOKENIZER_URL => degraded-not-dead: usage-less events are republished
    # with source="none" (reason="tokenizer_unset"), never a startup failure (ADR-030 AD-07).
    # Must be the full /tokenize URL (e.g. http://tokenizer:8000/tokenize).
    tokenizer_url: str = ""
    tokenizer_timeout_ms: int = 2000
