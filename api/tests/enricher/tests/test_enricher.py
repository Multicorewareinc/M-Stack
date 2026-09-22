"""Covers specs/event-enricher/spec.md: republish + field preservation, normalized usage
(provider/estimated/none + breakdown), idempotency, republish safety (no-ack/terminal/poison),
and the degraded/error tokenizer paths. Offline — MockTransport tokenizer + capturing FakeJS,
no live NATS."""

from __future__ import annotations

from prometheus_client import REGISTRY

from conftest import FakeMsg, raw_msg, response_event


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# --- Enriched republish + field preservation (S1) -------------------------------------------
async def test_response_republished_to_enriched_subject_preserving_fields(consumer_factory):
    consumer, js, _calls = consumer_factory(enriched_subject="gateway.events.enriched")
    ev = response_event(request_id="req-9", principal="u2", model="m3", status=201,
                        ts=99.0, duration_ms=5.5, stream=True, usage={"total_tokens": 42},
                        api_key_id="key-7", owner_id="owner-5",
                        body={"choices": [{"message": {"content": "should be dropped"}}]})
    await consumer._on_msg(FakeMsg(ev))

    assert len(js.published) == 1
    subject, enriched, _headers = js.published[0]
    assert subject == "gateway.events.enriched"
    assert enriched["type"] == "response"
    for field, expected in [("request_id", "req-9"), ("principal", "u2"), ("model", "m3"),
                            ("status", 201), ("ts", 99.0), ("duration_ms", 5.5), ("stream", True),
                            ("api_key_id", "key-7"), ("owner_id", "owner-5")]:
        assert enriched[field] == expected
    assert "body" not in enriched  # body is dropped


# --- Normalized usage: provider (S3) + breakdown + zero tokenizer calls ----------------------
async def test_provider_usage_preserved_with_breakdown_and_no_tokenizer_call(consumer_factory):
    consumer, js, calls = consumer_factory(tokenizer_url="http://tok/tokenize", tokenizer_tokens=999)
    ev = response_event(usage={"total_tokens": 150, "prompt_tokens": 100, "completion_tokens": 50})
    await consumer._on_msg(FakeMsg(ev))

    _subject, enriched, _headers = js.published[0]
    assert enriched["usage"] == {"total_tokens": 150, "source": "provider",
                                 "prompt_tokens": 100, "completion_tokens": 50}
    assert calls["n"] == 0  # provider path never calls the tokenizer


# --- Usage object without a numeric total falls through to estimation (S4-adjacent) ----------
async def test_usage_without_numeric_total_falls_through(consumer_factory):
    consumer, js, _calls = consumer_factory(tokenizer_url="http://tok/tokenize", tokenizer_tokens=11)
    ev = response_event(usage={"prompt_tokens": 5},  # no total_tokens
                        body={"choices": [{"message": {"content": "hi"}}]})
    await consumer._on_msg(FakeMsg(ev))
    _subject, enriched, _headers = js.published[0]
    assert enriched["usage"]["source"] == "estimated"
    assert enriched["usage"]["total_tokens"] == 11


# --- Estimated from body (S4) ---------------------------------------------------------------
async def test_missing_usage_estimated_from_body(consumer_factory):
    consumer, js, calls = consumer_factory(tokenizer_url="http://tok/tokenize", tokenizer_tokens=42)
    ev = response_event(usage=None, body={"choices": [{"message": {"content": "hello world"}}]})
    await consumer._on_msg(FakeMsg(ev))
    _subject, enriched, _headers = js.published[0]
    assert enriched["usage"] == {"total_tokens": 42, "source": "estimated"}
    assert calls["n"] == 1


# --- Un-countable: no usage, no body (S5) ---------------------------------------------------
async def test_no_usage_no_body_is_none_no_body(consumer_factory):
    consumer, js, _calls = consumer_factory(tokenizer_url="http://tok/tokenize")
    await consumer._on_msg(FakeMsg(response_event(usage=None, body=None)))
    _subject, enriched, _headers = js.published[0]
    assert enriched["usage"] == {"total_tokens": None, "source": "none", "reason": "no_body"}


# --- Non-response events are ignored (S2) ---------------------------------------------------
async def test_request_and_chunk_events_are_ignored(consumer_factory):
    consumer, js, _calls = consumer_factory()
    await consumer._on_msg(FakeMsg({"type": "request", "request_id": "r"}))
    await consumer._on_msg(FakeMsg({"type": "chunk", "request_id": "r", "seq": 1}))
    assert js.published == []


async def test_attribution_fields_null_when_absent_on_source(consumer_factory):
    # A response event with NO api_key_id/owner_id (e.g. published before the gateway emitted
    # them) republishes with both present as null — never omitted, never fabricated.
    consumer, js, _calls = consumer_factory()
    ev = response_event()
    ev.pop("api_key_id", None)  # ensure absent (the helper doesn't set them anyway)
    ev.pop("owner_id", None)
    await consumer._on_msg(FakeMsg(ev))

    _subject, enriched, _headers = js.published[0]
    assert "api_key_id" in enriched and enriched["api_key_id"] is None
    assert "owner_id" in enriched and enriched["owner_id"] is None


# --- Idempotent republish: outcome + header (S6/S7) -----------------------------------------
async def test_redelivery_dedupes_and_msgid_is_request_id(consumer_factory):
    consumer, js, _calls = consumer_factory()
    ev = response_event(request_id="dup-1")
    await consumer._on_msg(FakeMsg(ev))
    await consumer._on_msg(FakeMsg(ev))  # redelivery, same request_id
    assert len(js.published) == 1  # exactly one enriched event retained
    _subject, _enriched, headers = js.published[0]
    assert headers["Nats-Msg-Id"] == "dup-1"


# --- Republish safety: failure not acked; un-parseable acked; terminal metered (S7/S8/S10) --
async def test_republish_failure_is_not_acked(consumer_factory):
    consumer, _js, _calls = consumer_factory(publish_error=RuntimeError("stream down"), max_deliver=5)
    msg = FakeMsg(response_event(), num_delivered=1)
    await consumer._on_msg(msg)
    assert msg.acked is False  # not acked -> JetStream redelivers


async def test_unparseable_message_is_acked_and_skipped(consumer_factory):
    consumer, js, _calls = consumer_factory()
    msg = raw_msg(b"not json{")
    await consumer._on_msg(msg)
    assert msg.acked is True
    assert js.published == []


async def test_terminal_republish_failure_is_metered(consumer_factory):
    consumer, _js, _calls = consumer_factory(publish_error=RuntimeError("stream down"), max_deliver=3)
    before = _sample("enricher_republish_failures_total", terminal="true")
    msg = FakeMsg(response_event(), num_delivered=3)  # == max_deliver -> terminal
    await consumer._on_msg(msg)
    assert msg.acked is False
    assert _sample("enricher_republish_failures_total", terminal="true") - before == 1.0


# --- Tokenizer degraded/error (S11/S12) -----------------------------------------------------
async def test_tokenizer_unset_degrades_to_none(consumer_factory):
    consumer, js, calls = consumer_factory(tokenizer_url="")  # unset
    await consumer._on_msg(FakeMsg(response_event(usage=None,
                                                  body={"choices": [{"message": {"content": "hi"}}]})))
    _subject, enriched, _headers = js.published[0]
    assert enriched["usage"] == {"total_tokens": None, "source": "none", "reason": "tokenizer_unset"}
    assert calls["n"] == 0  # no tokenizer call when unset


async def test_tokenizer_error_degrades_to_none_and_meters(consumer_factory):
    before = _sample("enricher_tokenizer_fallback_total", outcome="error")
    consumer, js, _calls = consumer_factory(tokenizer_url="http://tok/tokenize", tokenizer_raises=True)
    await consumer._on_msg(FakeMsg(response_event(usage=None,
                                                  body={"choices": [{"message": {"content": "hi"}}]})))
    _subject, enriched, _headers = js.published[0]
    assert enriched["usage"] == {"total_tokens": None, "source": "none", "reason": "tokenizer_error"}
    assert _sample("enricher_tokenizer_fallback_total", outcome="error") - before == 1.0
