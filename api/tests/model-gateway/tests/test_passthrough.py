"""OpenAI-compatible passthrough: shape preserved, streaming unbuffered, routing."""

import json

import httpx

AUTH = {"Authorization": "Bearer k"}


def test_chat_passthrough_preserves_shape(make_client):
    canned = {"id": "chatcmpl-1", "object": "chat.completion", "choices": []}
    client, rec = make_client(lambda req: httpx.Response(200, json=canned), api_key="k", upstream_url="http://up")
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m1", "messages": []})
    assert r.status_code == 200
    assert r.json() == canned
    assert json.loads(rec.requests[0].content)["model"] == "m1"
    assert rec.requests[0].url.path == "/v1/chat/completions"


def test_streaming_passthrough_bytes(make_client):
    parts = [b'data: {"x":1}\n\n', b"data: [DONE]\n\n"]
    def h(req):
        async def body():
            for p in parts:
                yield p
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())
    client, rec = make_client(h, api_key="k", upstream_url="http://up")
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "m", "stream": True})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert r.content == b"".join(parts)


def test_streaming_forward_requests_usage(make_client):
    """ADR-030: without stream_options.include_usage, an OpenAI-compatible upstream never emits
    a trailing usage chunk, and router.py's own usage_from_sse_line() has nothing to find --
    every streamed response goes uncounted downstream no matter how NATS/enricher are wired."""
    client, rec = make_client(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=iter([b"data: [DONE]\n\n"])),
                               api_key="k", upstream_url="http://up")
    client.post("/v1/chat/completions", headers=AUTH, json={"model": "m", "stream": True})
    assert json.loads(rec.requests[0].content)["stream_options"] == {"include_usage": True}


def test_streaming_forward_respects_explicit_stream_options(make_client):
    """A caller's own stream_options (even include_usage: false) is a deliberate choice, not
    something this gateway silently reverses."""
    client, rec = make_client(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=iter([b"data: [DONE]\n\n"])),
                               api_key="k", upstream_url="http://up")
    client.post("/v1/chat/completions", headers=AUTH,
                json={"model": "m", "stream": True, "stream_options": {"include_usage": False}})
    assert json.loads(rec.requests[0].content)["stream_options"] == {"include_usage": False}


def test_completions_forwards(make_client):
    client, rec = make_client(lambda req: httpx.Response(200, json={"object": "text_completion"}), api_key="k", upstream_url="http://up")
    r = client.post("/v1/completions", headers=AUTH, json={"model": "m", "prompt": "x"})
    assert r.status_code == 200
    assert rec.requests[0].url.path == "/v1/completions"


def test_missing_model_400_no_forward(make_client):
    client, rec = make_client(api_key="k")
    r = client.post("/v1/chat/completions", headers=AUTH, json={"messages": []})
    assert r.status_code == 400
    assert rec.requests == []


def test_malformed_json_400_no_forward(make_client):
    client, rec = make_client(api_key="k")
    r = client.post("/v1/chat/completions", headers={**AUTH, "Content-Type": "application/json"}, content=b"{bad")
    assert r.status_code == 400
    assert rec.requests == []


def test_models_lists_routes(make_client):
    routes = json.dumps({"m1": "http://a", "m2": {"url": "http://b", "type": "anthropic"}})
    client, rec = make_client(api_key="k", model_routes=routes)
    r = client.get("/v1/models", headers=AUTH)
    assert {m["id"] for m in r.json()["data"]} == {"m1", "m2"}
