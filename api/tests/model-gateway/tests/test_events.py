"""Event backbone publisher (ADR-007/008/009). Uses an injected FakePublisher so no
NATS is needed; asserts the async strain without touching the sync forwarding bytes."""

import httpx

from conftest import VERIFY_API_KEY_ID, VERIFY_ORG_ID, VERIFY_OWNER_ID

AUTH = {"Authorization": "Bearer test-key"}


def _sse(parts):
    def h(_req):
        async def body():
            for p in parts:
                yield p
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())
    return h


def test_inert_when_unconfigured(make_client):
    # No EVENT_BACKBONE_URL => no publisher; path identical to no feature.
    client, rec = make_client(lambda req: httpx.Response(200, json={"ok": True}), api_key="test-key", upstream_url="http://up")
    assert client.app.state.event_publisher is None
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m", "messages": []})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_buffered_emits_request_and_response(make_events_client):
    client, pub, up = make_events_client()  # default handler returns usage total_tokens=7
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m1"})
    assert r.status_code == 200
    types = [e["type"] for e in pub.events]
    assert types == ["request", "response"]  # request emitted before response
    req, resp = pub.events
    assert req["model"] == "m1" and req["principal"] == VERIFY_ORG_ID and req["path"] == "/v1/chat/completions"
    assert resp["status"] == 200 and resp["usage"] == {"total_tokens": 7} and resp["stream"] is False
    assert "request_id" in req and resp["request_id"] == req["request_id"]


def test_response_event_carries_principal(make_events_client):
    client, pub, up = make_events_client()
    client.post("/v1/chat/completions", headers=AUTH, json={"model": "m1"})
    req, resp = pub.events
    assert resp["principal"] == req["principal"] == VERIFY_ORG_ID


def test_response_event_principal_null_when_absent():
    from events import response_event

    ev = response_event({"request_id": "r1"}, status=200, usage=None, duration_ms=1.0, stream=False)
    assert ev["principal"] is None


def test_events_carry_api_key_attribution(make_events_client):
    client, pub, up = make_events_client()
    client.post("/v1/chat/completions", headers=AUTH, json={"model": "m1"})
    req, resp = pub.events
    assert req["api_key_id"] == VERIFY_API_KEY_ID and req["owner_id"] == VERIFY_OWNER_ID
    assert resp["api_key_id"] == req["api_key_id"] and resp["owner_id"] == req["owner_id"]


def test_events_attribution_null_when_absent():
    from events import request_event, response_event

    req = request_event({"request_id": "r1"}, None)
    resp = response_event({"request_id": "r1"}, status=200, usage=None, duration_ms=1.0, stream=False)
    assert req["api_key_id"] is None and req["owner_id"] is None
    assert resp["api_key_id"] is None and resp["owner_id"] is None


def test_stream_tees_and_completes(make_events_client):
    parts = [b'data: {"x":1}\n\n', b'data: {"usage":{"total_tokens":9}}\n\n', b"data: [DONE]\n\n"]
    client, pub, up = make_events_client(_sse(parts))
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m", "stream": True})
    assert r.status_code == 200
    assert r.content == b"".join(parts)  # client bytes unchanged
    chunks = pub.of_type("chunk")
    assert [c["seq"] for c in chunks] == [0, 1, 2]  # monotonic
    completions = pub.of_type("response")
    assert len(completions) == 1  # exactly one completion event
    done = completions[0]
    assert done["stream"] is True and done["partial"] is False
    assert done["usage"] == {"total_tokens": 9} and done["chunks"] == 3


def test_body_capture_flag(make_events_client):
    # off (default): no prompt/response text anywhere.
    client, pub, _ = make_events_client()
    client.post("/v1/chat/completions", headers=AUTH, json={"model": "m", "messages": [{"x": 1}]})
    assert all("body" not in e and "data" not in e for e in pub.events)

    # on: request event carries the body; chunk events carry data.
    from conftest import FakePublisher

    parts = [b'data: {"x":1}\n\n', b"data: [DONE]\n\n"]
    client2, pub2, _ = make_events_client(_sse(parts), publisher=FakePublisher(capture_bodies=True), event_capture_bodies=True)
    client2.post("/v1/chat/completions", headers=AUTH, json={"model": "m", "stream": True})
    req = pub2.of_type("request")[0]
    assert req["body"] == {"model": "m", "stream": True}
    assert all("data" in c for c in pub2.of_type("chunk"))


def test_error_path_emits_response(make_events_client):
    def boom(_req):
        raise httpx.ConnectError("upstream down")
    client, pub, _ = make_events_client(boom)
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 503  # ServiceUnavailableError
    types = [e["type"] for e in pub.events]
    assert types == ["request", "response"]  # failure still emits a matched response
    assert pub.events[1]["status"] == 503 and pub.events[1]["usage"] is None


def test_publish_failure_is_isolated(make_events_client):
    from conftest import FakePublisher

    client, pub, _ = make_events_client(publisher=FakePublisher(boom=True))
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m"})
    assert r.status_code == 200  # emit is total: a publisher fault never reaches the client


async def test_safe_publish_gives_request_and_response_distinct_msg_ids():
    # Regression: request_event and response_event share a bare request_id, and
    # _safe_publish used to key Nats-Msg-Id on that alone for anything but a chunk. JetStream
    # dedups by Nats-Msg-Id within its duplicate window regardless of message content, so the
    # response (published moments after the request, same key) was silently discarded as a
    # "duplicate" and never reached the durable stream -- only ephemeral core subscribers,
    # which aren't gated by that storage-layer dedup. This is what all of billing/enricher's
    # empty-tables symptoms traced back to. FakePublisher (used elsewhere in this file) never
    # exercises this path at all, since it bypasses EventPublisher/_safe_publish entirely.
    from events import EventPublisher, request_event, response_event

    class FakeJS:
        def __init__(self):
            self.published = []  # (subject, headers)

        async def publish(self, subject, data, headers=None):
            self.published.append((subject, headers))

    pub = EventPublisher("nats://test", stream_name="S", subject="s", capture_bodies=False, max_inflight=10)
    pub._js = FakeJS()  # bypass connect(): no real NATS needed

    ctx = {"request_id": "r1", "principal": "p", "model": "m", "path": "/v1/chat/completions"}
    await pub._safe_publish(request_event(ctx, None))
    await pub._safe_publish(response_event(ctx, status=200, usage={"total_tokens": 9}, duration_ms=1.0, stream=False))

    msg_ids = [h["Nats-Msg-Id"] for _subject, h in pub._js.published]
    assert len(msg_ids) == len(set(msg_ids)) == 2  # distinct -- neither can dedup the other


def test_abort_emits_partial(make_events_client):
    def h(_req):
        async def body():
            yield b'data: {"x":1}\n\n'
            raise RuntimeError("upstream stream broke")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())
    client, pub, _ = make_events_client(h)
    try:
        with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={"model": "m", "stream": True}) as r:
            for _ in r.iter_bytes():
                pass
    except Exception:  # noqa: BLE001 — the broken upstream stream may surface on read
        pass
    completions = pub.of_type("response")
    assert len(completions) == 1 and completions[0]["partial"] is True
