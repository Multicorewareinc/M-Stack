"""Portability and robustness of the state viewer: the things that differ from one machine to the next
(odd paths, unwritable folders, wrong files, ports, packaging), plus a check against what the *real*
RKE2 backend records rather than a script written for the viewer.

Companion to tests/test_state_viewer.py (the API contract); kept separate because test modules here are
imported in isolation (see `--import-mode=importlib` in pyproject.toml), so helpers are not shared.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from multistack import RKE2Cluster, RKE2Node
from multistack.backends.rke2_client import RKE2Backend, RKE2Error
from multistack.state import StateConfig, StateManager
from multistack.state.viewer import create_server
from multistack.state.viewer import server as viewer_server
from multistack.state.viewer.server import (
    MAX_HISTORY,
    StateReader,
    allowed_hosts,
    default_ui_dir,
    main,
    resolve_db_path,
)


def make_state(tmp_path) -> Path:
    path = tmp_path / "state.db"
    manager = StateManager(StateConfig(db_path=str(path)))
    manager.start("edge-cluster", "cluster")
    manager.mark_healthy("edge-cluster")
    manager.close()
    return path


class Viewer:
    def __init__(self, db_path):
        self.server = create_server(db_path, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"null")

    def get_with_host(self, host_header, path="/api/summary"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host_header)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# -- paths ------------------------------------------------------------------------------------
AWKWARD_DIRS = ["John Smith", "weird#dir", "100%real", "ünïcode-ディレクトリ", "a&b=c"]
if os.name != "nt":
    AWKWARD_DIRS.append("what?")  # '?' is not a legal character in a Windows path


@pytest.mark.parametrize("dirname", AWKWARD_DIRS)
def test_reads_a_database_whatever_characters_its_path_has(tmp_path, dirname):
    """Home directories with spaces, '#', '%' or non-ASCII names are normal; a hand-built `file:` URI
    cut the path at '#' and opened the wrong file."""
    folder = tmp_path / dirname
    folder.mkdir()
    path = make_state(folder)

    v = Viewer(path)
    try:
        assert [d["name"] for d in v.get("/api/deployments")[1]] == ["edge-cluster"]
    finally:
        v.close()


def test_a_path_with_a_hash_never_opens_the_truncated_path_instead(tmp_path):
    """The dangerous half of the '#' bug: with a different database at the truncated path, the viewer
    must still read the file it was given."""
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy = StateManager(StateConfig(db_path=str(decoy_dir / "state.db")))
    decoy.start("WRONG-FILE", "cluster")
    decoy.close()
    real_dir = tmp_path / "decoy#real"
    real_dir.mkdir()
    real = StateManager(StateConfig(db_path=str(real_dir / "state.db")))
    real.start("right-file", "cluster")
    real.close()

    assert [d["name"] for d in StateReader(real_dir / "state.db").deployments()] == ["right-file"]


# -- folders the viewer cannot write to ----------------------------------------------------------
def test_reads_through_a_private_copy_when_the_folder_is_not_writable(tmp_path, monkeypatch):
    """A WAL database needs a -shm file beside it, which a reader cannot create in a read-only mount or
    someone else's directory ("unable to open database file"). Simulated by making the direct open fail,
    since permission bits are not reliable to test (root, Windows)."""
    manager = StateManager(StateConfig(db_path=str(tmp_path / "state.db")))
    manager.start("edge", "cluster")
    manager.mark_healthy("edge")
    # deliberately NOT closed: the newest rows may still sit in the -wal file, which the copy must include

    attempts = []

    def refuse(self):
        attempts.append(1)
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(StateReader, "_connect", refuse)
    rows = StateReader(tmp_path / "state.db").deployments()

    assert attempts, "the direct open was never attempted"
    assert [d["name"] for d in rows] == ["edge"]
    assert rows[0]["latest_health_check"]["status"] == "healthy"
    manager.close()


def test_an_unreadable_folder_reports_why_instead_of_a_bare_sqlite_error(tmp_path, monkeypatch):
    (tmp_path / "state.db").write_bytes(b"x")

    def refuse(self):
        raise sqlite3.OperationalError("unable to open database file")

    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(StateReader, "_connect", refuse)
    monkeypatch.setattr(viewer_server.shutil, "copy2", deny)
    v = Viewer(tmp_path / "state.db")
    try:
        status, body = v.get("/api/deployments")
        assert status == 503 and "cannot be read" in body["error"]["message"]
    finally:
        v.close()


# -- wrong files ----------------------------------------------------------------------------------
def test_a_foreign_sqlite_file_says_what_it_is(tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    v = Viewer(other)
    try:
        status, body = v.get("/api/deployments")
        assert status == 503
        assert "not a multistack state database" in body["error"]["message"]
        assert "--db" in body["error"]["message"]  # tells the user how to point it elsewhere
    finally:
        v.close()


def test_a_non_sqlite_file_says_so(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is plain text, not a database" * 40)
    v = Viewer(junk)
    try:
        status, body = v.get("/api/info")
        assert status == 503 and "is not a SQLite database" in body["error"]["message"]
    finally:
        v.close()


def test_a_zero_byte_file_is_an_empty_store_not_a_crash(tmp_path):
    empty = tmp_path / "state.db"
    empty.write_bytes(b"")
    v = Viewer(empty)
    try:
        info = v.get("/api/info")[1]
        assert info["exists"] is True and info["deployments"] == 0 and info["health_checks"] == 0
        assert v.get("/api/deployments")[1] == [] and v.get("/api/summary")[1]["total"] == 0
    finally:
        v.close()


# -- network ----------------------------------------------------------------------------------------
def test_rejects_requests_whose_host_header_is_not_loopback(tmp_path):
    """The viewer has no auth. A page on another site could reach it by pointing a DNS name at
    127.0.0.1 (DNS rebinding) -- but that request carries the attacker's name in `Host`."""
    v = Viewer(make_state(tmp_path))
    try:
        assert v.get_with_host("evil.example.com") == 403
        assert v.get_with_host(f"evil.example.com:{v.port}") == 403
        assert v.get_with_host(f"127.0.0.1:{v.port}") == 200
        assert v.get_with_host(f"localhost:{v.port}") == 200
        assert v.get_with_host(f"LOCALHOST:{v.port}") == 200
        assert v.get_with_host(f"[::1]:{v.port}") == 200
    finally:
        v.close()


def test_host_allow_list_follows_the_bind_address():
    assert "evil.example.com" not in allowed_hosts("127.0.0.1")
    assert "192.168.1.5" in allowed_hosts("192.168.1.5") and "localhost" in allowed_hosts(
        "192.168.1.5"
    )
    assert (
        allowed_hosts("0.0.0.0") is None and allowed_hosts("::") is None
    )  # an explicit, warned opt-out


def test_a_busy_port_gives_a_clear_message_and_a_failing_exit_code(tmp_path, capsys):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    try:
        code = main(["--db", str(tmp_path / "state.db"), "--port", str(held.getsockname()[1])])
    finally:
        held.close()
    assert code == 2
    assert "Pick a free port with --port" in capsys.readouterr().err


def test_a_second_viewer_cannot_silently_share_a_busy_port(tmp_path):
    """On Windows SO_REUSEADDR lets a second process bind a port that is already listening, so two
    viewers shared one port and 'port in use' was never reported."""
    first = create_server(tmp_path / "state.db", port=0)
    try:
        with pytest.raises(OSError):
            create_server(tmp_path / "state.db", port=first.server_address[1])
    finally:
        first.server_close()


# -- bounds -----------------------------------------------------------------------------------------
def test_a_deployments_history_is_capped_so_one_noisy_component_cannot_flood_the_page(tmp_path):
    path = make_state(tmp_path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO health_checks (deployment_name, status, detail, checked_at) "
        "VALUES ('edge-cluster', 'healthy', ?, '2026-10-08T00:00:00+00:00')",
        [(f"probe {i}",) for i in range(MAX_HISTORY + 100)],
    )
    conn.commit()
    conn.close()

    history = StateReader(path).deployment("edge-cluster")["health_checks"]
    assert len(history) == MAX_HISTORY
    assert history[0]["detail"] == f"probe {MAX_HISTORY + 99}"  # the newest are kept


# -- packaging ----------------------------------------------------------------------------------------
def test_the_ui_ships_inside_the_package_and_is_complete():
    """`pip install` must give a working viewer with no Node and no checkout. That needs the built UI in
    the package, with every file index.html points at (a stale or half-copied build is a blank page).
    """
    shipped = viewer_server._PACKAGED_UI
    assert (shipped / "index.html").is_file(), "run: cd ui && npm run build:sdk -w state-viewer"
    assert default_ui_dir() == shipped  # the packaged build wins over a dev build
    html = (shipped / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
    assert referenced, "index.html references no assets"
    for ref in referenced:
        assert (shipped / ref.lstrip("/")).is_file(), f"missing {ref}"


def test_the_build_is_declared_as_package_data_and_a_command_is_installed():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert "state/viewer/web/**/*" in text  # without this a wheel omits the UI
    assert "multistack-state-viewer" in text


# -- the real thing -------------------------------------------------------------------------------------
def _stub_out_provisioning(backend, monkeypatch, failing_agents=()):
    """The same stubs tests/backends/test_rke2_client.py uses: everything that would shell out is a
    no-op, so the backend's real create() (and its @track_create recording) runs in-process."""
    monkeypatch.setattr(backend, "check_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(backend, "_bootstrap_server", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_wait_for_node_token", lambda node: None)
    monkeypatch.setattr(backend, "_fetch_kubeconfig", lambda node: "fake-kubeconfig")
    monkeypatch.setattr(backend, "_write_kubeconfig_file", lambda path, kubeconfig: None)
    monkeypatch.setattr(backend, "_prepare_control_plane", lambda node, path: None)
    monkeypatch.setattr(backend, "_unlink_kubectl", lambda node: None)

    def bootstrap_agent(node, token, version, first_server, pin_node_ip=True):
        if node.address in failing_agents:
            raise RuntimeError(f"simulated join failure on {node.address}")

    monkeypatch.setattr(backend, "_bootstrap_agent", bootstrap_agent)


def _cluster(name):
    return RKE2Cluster(
        name=name,
        nodes=[
            RKE2Node(address="server-1", role="server"),
            RKE2Node(address="agent-1", role="agent"),
        ],
    )


def test_the_viewer_shows_what_the_real_rke2_backend_recorded(tmp_path, monkeypatch):
    # tests/conftest.py points MULTISTACK_STATE_DB at tmp_path/state.db for every test, so the backend's
    # own @track_create writes there -- the same file the viewer is then asked to read.
    backend = RKE2Backend(
        state_dir=str(tmp_path / "rke2"),
        poll_interval=0,
        poll_timeout=1,
        default_kubeconfig_path=str(tmp_path / "kube" / "config"),
    )
    _stub_out_provisioning(backend, monkeypatch)
    backend.create(_cluster("prod-edge"))

    _stub_out_provisioning(backend, monkeypatch, failing_agents=("agent-1",))
    with pytest.raises(RKE2Error):
        backend.create(_cluster("broken-edge"))

    v = Viewer(resolve_db_path())  # no --db: the same resolution the SDK's backends use
    try:
        by_name = {d["name"]: d for d in v.get("/api/deployments")[1]}
        assert by_name["prod-edge"]["component_type"] == "cluster"
        assert by_name["prod-edge"]["status"] == "provisioned"
        assert by_name["prod-edge"]["latest_health_check"]["status"] == "healthy"
        assert by_name["broken-edge"]["status"] == "failed"
        assert "simulated join failure on agent-1" in by_name["broken-edge"]["error"]

        detail = v.get("/api/deployment?name=broken-edge")[1]
        assert detail["health_checks"][0]["status"] == "unhealthy"

        summary = v.get("/api/summary")[1]
        assert {r["status"]: r["count"] for r in summary["by_status"]} == {
            "provisioned": 1,
            "failed": 1,
        }
    finally:
        v.close()
