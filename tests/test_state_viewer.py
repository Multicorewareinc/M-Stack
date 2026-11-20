"""Tests for the state viewer: a read-only HTTP view over the SQLite state db.

Each test builds a real state database through `StateManager` (so the schema is
exactly what the SDK writes), starts the viewer on a free loopback port in a
thread, and talks to it over HTTP. Nothing touches a cluster."""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from multistack.state import StateConfig, StateManager
from multistack.state.viewer import create_server
from multistack.state.viewer.server import resolve_db_path


def make_state(tmp_path):
    path = tmp_path / "state.db"
    manager = StateManager(StateConfig(db_path=str(path)))
    manager.start("edge-cluster", "cluster")
    manager.mark_healthy("edge-cluster", kubeconfig_path="/tmp/kubeconfig")
    manager.start("longhorn", "storage")
    manager.mark_failed("longhorn", "helm timed out after 600s")
    manager.start("default/chat-route", "route")  # a name containing "/"
    manager.mark_degraded("default/chat-route", "backend slow")
    manager.start("tmp-queue", "queue")
    manager.remove("tmp-queue")
    manager.close()
    return path


class Viewer:
    def __init__(self, db_path, static_dir=None):
        self.server = create_server(db_path, port=0, static_dir=static_dir)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as resp:
                return resp.status, json.loads(resp.read()), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"null"), err.headers

    def raw(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def viewer(tmp_path):
    v = Viewer(make_state(tmp_path))
    yield v
    v.close()


def test_deployments_lists_every_row_with_its_latest_health_check(viewer):
    status, body, headers = viewer.get("/api/deployments")

    assert status == 200 and headers["Cache-Control"] == "no-store"
    by_name = {d["name"]: d for d in body}
    assert set(by_name) == {"edge-cluster", "longhorn", "default/chat-route"}
    assert by_name["edge-cluster"]["status"] == "provisioned"
    assert by_name["edge-cluster"]["kubeconfig_path"] == "/tmp/kubeconfig"
    assert by_name["edge-cluster"]["latest_health_check"]["status"] == "healthy"
    assert by_name["longhorn"]["status"] == "failed"
    assert by_name["longhorn"]["error"] == "helm timed out after 600s"
    assert by_name["longhorn"]["latest_health_check"]["detail"] == "helm timed out after 600s"


def test_a_deleted_deployment_is_gone_not_listed(viewer):
    names = [d["name"] for d in viewer.get("/api/deployments")[1]]
    assert "tmp-queue" not in names


def test_filters_by_status_component_type_and_search(viewer):
    assert [d["name"] for d in viewer.get("/api/deployments?status=failed")[1]] == ["longhorn"]
    assert [d["name"] for d in viewer.get("/api/deployments?component_type=cluster")[1]] == [
        "edge-cluster"
    ]
    assert [d["name"] for d in viewer.get("/api/deployments?search=timed%20out")[1]] == [
        "longhorn"
    ]  # error text
    assert [d["name"] for d in viewer.get("/api/deployments?search=EDGE")[1]] == [
        "edge-cluster"
    ]  # case-insensitive


def test_search_treats_like_wildcards_literally(viewer):
    assert viewer.get("/api/deployments?search=%25")[1] == []
    assert viewer.get("/api/deployments?search=_")[1] == []


def test_summary_counts_by_status_and_type(viewer):
    _, body, _ = viewer.get("/api/summary")

    assert body["total"] == 3
    assert {r["status"]: r["count"] for r in body["by_status"]} == {
        "provisioned": 1,
        "failed": 1,
        "degraded": 1,
    }
    assert {r["component_type"]: r["count"] for r in body["by_component_type"]} == {
        "cluster": 1,
        "storage": 1,
        "route": 1,
    }
    assert body["last_updated"]


def test_deployment_detail_by_name_including_names_with_a_slash(viewer):
    status, body, _ = viewer.get("/api/deployment?name=default%2Fchat-route")

    assert status == 200
    assert body["name"] == "default/chat-route" and body["status"] == "degraded"
    assert [h["status"] for h in body["health_checks"]] == ["degraded"]


def test_deployment_detail_errors(viewer):
    assert viewer.get("/api/deployment?name=nope")[0] == 404
    assert viewer.get("/api/deployment")[0] == 422


def test_health_history_is_newest_first_and_filterable(viewer, tmp_path):
    manager = StateManager(StateConfig(db_path=str(tmp_path / "state.db")))
    manager.mark_healthy("longhorn")  # a later recovery after the failure
    manager.close()

    _, history, _ = viewer.get("/api/deployment?name=longhorn")
    assert [h["status"] for h in history["health_checks"]] == ["healthy", "unhealthy"]

    _, feed, _ = viewer.get("/api/health-checks")
    assert feed[0]["deployment_name"] == "longhorn" and feed[0]["status"] == "healthy"
    assert feed[0]["component_type"] == "storage"
    assert [
        h["deployment_name"] for h in viewer.get("/api/health-checks?deployment=edge-cluster")[1]
    ] == ["edge-cluster"]
    assert all(
        h["status"] == "unhealthy" for h in viewer.get("/api/health-checks?status=unhealthy")[1]
    )
    assert len(viewer.get("/api/health-checks?limit=1")[1]) == 1
    assert viewer.get("/api/health-checks?limit=abc")[0] == 422


def test_sees_changes_made_after_it_started(viewer, tmp_path):
    assert viewer.get("/api/deployments?status=failed")[1][0]["name"] == "longhorn"

    manager = StateManager(StateConfig(db_path=str(tmp_path / "state.db")))
    manager.mark_healthy("longhorn")
    manager.close()

    assert viewer.get("/api/deployments?status=failed")[1] == []


def test_is_read_only(viewer, tmp_path):
    before = (
        sqlite3.connect(tmp_path / "state.db")
        .execute("select count(*) from deployments")
        .fetchone()[0]
    )

    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert viewer.raw("/api/deployments", method=method)[0] == 405

    after = (
        sqlite3.connect(tmp_path / "state.db")
        .execute("select count(*) from deployments")
        .fetchone()[0]
    )
    assert before == after == 3


def test_reader_connection_cannot_write(tmp_path):
    from multistack.state.viewer.server import StateReader

    reader = StateReader(make_state(tmp_path))
    conn = reader._connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM deployments")
    conn.close()


def test_a_missing_database_is_empty_not_an_error_and_is_not_created(tmp_path):
    missing = tmp_path / "nested" / "state.db"
    v = Viewer(missing)
    try:
        info = v.get("/api/info")[1]
        assert info["exists"] is False and info["deployments"] == 0
        assert v.get("/api/deployments")[1] == []
        assert v.get("/api/summary")[1]["total"] == 0
        assert v.get("/api/health-checks")[1] == []
    finally:
        v.close()
    assert not missing.exists() and not missing.parent.exists()  # the viewer never creates it


def test_an_unreadable_database_is_a_503_with_the_reason(tmp_path):
    bad = tmp_path / "state.db"
    bad.write_bytes(b"this is not a sqlite database" * 50)
    v = Viewer(bad)
    try:
        status, body, _ = v.get("/api/deployments")
        assert status == 503 and "could not be read" in body["error"]["message"]
    finally:
        v.close()


def test_info_reports_path_and_counts(viewer, tmp_path):
    info = viewer.get("/api/info")[1]
    assert info["exists"] is True and info["db_path"] == str(tmp_path / "state.db")
    assert info["deployments"] == 3 and info["health_checks"] == 3


def test_unknown_api_path_is_404(viewer):
    assert viewer.get("/api/nope")[0] == 404


def test_serves_the_built_ui_with_spa_fallback_and_no_path_escape(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>shell</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("top secret")
    v = Viewer(make_state(tmp_path), static_dir=dist)
    try:
        assert v.raw("/")[1] == b"<html>shell</html>"
        _status, body, headers = v.raw("/assets/app.js")
        assert body == b"console.log(1)" and "javascript" in headers["Content-Type"]
        assert v.raw("/deployments/edge-cluster")[1] == b"<html>shell</html>"  # client-side route
        assert b"top secret" not in v.raw("/..%2Fsecret.txt")[1]
        assert b"top secret" not in v.raw("/../secret.txt")[1]
    finally:
        v.close()


def test_without_a_ui_build_the_root_explains_and_the_api_still_works(viewer):
    status, body, _ = viewer.get("/")
    assert status == 404 and "UI build" in body["error"]["message"]
    assert viewer.get("/api/summary")[0] == 200


def test_db_path_precedence_matches_the_backends(monkeypatch, tmp_path):
    monkeypatch.setenv("MULTISTACK_STATE_DB", str(tmp_path / "env.db"))
    assert resolve_db_path() == tmp_path / "env.db"
    assert resolve_db_path(str(tmp_path / "flag.db")) == tmp_path / "flag.db"  # --db wins
    monkeypatch.delenv("MULTISTACK_STATE_DB")
    assert resolve_db_path().name == "state.db" and resolve_db_path().parent.name == ".multistack"
