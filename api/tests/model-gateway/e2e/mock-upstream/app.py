"""Mock inference upstream (stdlib only, no dependencies). Mimics a vLLM
OpenAI-compatible server plus an Anthropic Messages endpoint, with deterministic
canned responses, so the gateway's passthrough AND adapter paths test live.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _chat_obj(model):
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello from mock vLLM"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in chunks:
            self.wfile.write(c.encode())
            self.wfile.flush()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok"})
        if self.path == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": "llama-3-8b", "object": "model", "owned_by": "mock-vllm"}]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        body = self._read_json()
        if self.path == "/v1/chat/completions":
            if body.get("stream"):
                chunks = []
                for piece in ["Hello", " from", " mock", " vLLM"]:
                    chunk = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                    chunks.append(f"data: {json.dumps(chunk)}\n\n")
                chunks.append("data: [DONE]\n\n")
                return self._sse(chunks)
            return self._json(200, _chat_obj(body.get("model", "mock")))
        if self.path == "/v1/completions":
            return self._json(200, {"id": "cmpl-mock", "object": "text_completion", "created": int(time.time()), "model": body.get("model", "mock"), "choices": [{"index": 0, "text": "Hello from mock vLLM", "finish_reason": "stop"}]})
        if self.path == "/v1/messages":
            if body.get("stream"):
                return self._sse([
                    'data: {"type":"message_start"}\n\n',
                    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hello"}}\n\n',
                    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n',
                    'data: {"type":"message_stop"}\n\n',
                ])
            return self._json(200, {"id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model", "claude"), "content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 5}})
        self._json(404, {"error": "not found"})

    def log_message(self, *_args):
        pass  # quiet


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
