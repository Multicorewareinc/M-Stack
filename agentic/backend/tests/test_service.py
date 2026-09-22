"""Tests for the HTTP service (main/router/service/errors/dependencies) --
offline, the orchestration graph stubbed via the injected FakeGraph (see
conftest.py). Tests for the tool layer itself live in test_mcp_server.py."""

from __future__ import annotations

from conftest import FakeGraph


def test_health_requires_no_auth(client_factory):
    resp = client_factory().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_metrics_requires_no_auth(client_factory):
    resp = client_factory().get("/metrics")
    assert resp.status_code == 200


def test_session_route_without_api_key_is_rejected(client_factory):
    resp = client_factory().post("/v1/sessions", json={"message": "hi"})
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "unauthorized"


def test_session_route_with_wrong_api_key_is_rejected(client_factory):
    resp = client_factory().post(
        "/v1/sessions", json={"message": "hi"}, headers={"X-API-Key": "wrong"}
    )
    assert resp.status_code == 401


def test_unconfigured_api_key_fails_closed(client_factory):
    client = client_factory(service_api_key=None)
    resp = client.post("/v1/sessions", json={"message": "hi"}, headers={"X-API-Key": "anything"})
    assert resp.status_code == 500
    assert resp.json()["error"]["type"] == "configuration_error"


def test_start_session_returns_needs_input_for_a_clarifying_question(client_factory):
    graph = FakeGraph(responses={})  # falls through to FakeGraph's default echo, no script
    resp = client_factory(graph).post(
        "/v1/sessions", json={"message": "set up a cluster"}, headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "needs_input"
    assert body["message"] == "echo: set up a cluster"
    assert body["script"] is None
    assert body["session_id"]


def test_continue_session_returns_resolved_once_a_script_is_produced(client_factory):
    session_id = "sess-1"
    graph = FakeGraph(
        responses={
            session_id: {"reply": "Here's your cluster script.", "script": "cluster = RKE2Cluster(...)"}
        }
    )
    resp = client_factory(graph).post(
        f"/v1/sessions/{session_id}/messages",
        json={"message": "one server node at 10.0.0.11"},
        headers={"X-API-Key": "test-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["message"] == "Here's your cluster script."
    assert body["script"] == "cluster = RKE2Cluster(...)"


def test_get_unknown_session_is_not_found(client_factory):
    resp = client_factory().get("/v1/sessions/unknown-session", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found"


def test_get_session_reflects_a_previously_generated_script(client_factory):
    session_id = "sess-2"
    graph = FakeGraph(
        states={session_id: {"messages": ["seed"], "reply": "done", "script": "cluster = RKE2Cluster(...)"}}
    )
    resp = client_factory(graph).get(f"/v1/sessions/{session_id}", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["script"] == "cluster = RKE2Cluster(...)"


def test_get_session_still_gathering_input_has_no_script(client_factory):
    session_id = "sess-3"
    graph = FakeGraph(states={session_id: {"messages": ["seed"], "reply": "what's the node address?", "script": None}})
    resp = client_factory(graph).get(f"/v1/sessions/{session_id}", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "needs_input"
    assert body["script"] is None


class SlowGraph(FakeGraph):
    """A turn that takes as long as a real model call, as far as the
    server can tell."""

    def send_message(self, thread_id: str, message: str) -> dict:
        import time

        time.sleep(1.5)
        return super().send_message(thread_id, message)


def test_a_turn_in_progress_does_not_stall_other_requests(client_factory):
    """The model call is synchronous. On an `async def` route it ran on
    the event loop and every other request waited for it -- /health took
    53s behind a single live turn."""
    import threading
    import time

    # Inside `with`, every request shares one event loop, as they do on the
    # real server; outside it, each request gets its own and none can block.
    with client_factory(SlowGraph()) as client:
        turn = threading.Thread(
            target=client.post,
            args=("/v1/sessions",),
            kwargs={"json": {"message": "hi"}, "headers": {"X-API-Key": "test-key"}},
        )
        turn.start()
        time.sleep(0.3)  # let the turn reach the slow part
        started = time.monotonic()
        resp = client.get("/health")
        waited = time.monotonic() - started
        turn.join()

    assert resp.status_code == 200
    assert waited < 1.0, f"/health waited {waited:.2f}s behind a turn in progress"
