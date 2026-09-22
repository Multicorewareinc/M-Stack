"""Anthropic adapter: translation functions + a routed request end to end."""

import json

import httpx

from adapters import to_anthropic_request, to_openai_response, translate_stream

AUTH = {"Authorization": "Bearer k"}


def test_to_anthropic_request_system_and_default_max_tokens():
    payload = {"model": "c", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]}
    out = to_anthropic_request(payload)
    assert out["system"] == "s"
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["max_tokens"] == 1024


def test_to_openai_response_maps_content_finish_usage():
    aj = {"id": "m1", "content": [{"type": "text", "text": "hi"}], "stop_reason": "max_tokens", "usage": {"input_tokens": 1, "output_tokens": 2}}
    out = to_openai_response(aj, model="c")
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "hi"
    assert out["choices"][0]["finish_reason"] == "length"
    assert out["usage"]["total_tokens"] == 3


async def test_translate_stream():
    async def lines():
        for line in [
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
            'data: {"type":"message_stop"}',
        ]:
            yield line

    chunks = [c async for c in translate_stream(lines(), model="c")]
    text = "".join(c.decode() for c in chunks)
    assert '"content": "Hi"' in text
    assert "data: [DONE]" in text


def test_anthropic_route_translates(make_client):
    resp = {"id": "msg_1", "content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 5}}

    def h(req):
        assert req.url.path == "/v1/messages"
        assert req.headers.get("x-api-key") == "ak"
        assert "max_tokens" in json.loads(req.content)
        return httpx.Response(200, json=resp)

    routes = json.dumps({"claude": {"url": "http://anthropic", "type": "anthropic", "api_key": "ak"}})
    client, rec = make_client(h, api_key="k", model_routes=routes)
    r = client.post("/v1/chat/completions", headers=AUTH, json={"model": "claude", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello"


def test_anthropic_completions_400_no_forward(make_client):
    routes = json.dumps({"claude": {"url": "http://anthropic", "type": "anthropic", "api_key": "ak"}})
    client, rec = make_client(api_key="k", model_routes=routes)
    r = client.post("/v1/completions", headers=AUTH, json={"model": "claude", "prompt": "x"})
    assert r.status_code == 400
    assert rec.requests == []
