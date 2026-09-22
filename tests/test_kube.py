"""Tests for the shared cluster primitives in `multistack/kube.py`.

These existed four times over before — in the MinIO backend, the vLLM
backend, the Longhorn driver, and again in every example — and had
drifted: two closed stdin and two didn't, two raised a typed error on
timeout and two let `subprocess.TimeoutExpired` escape. Now there is one
implementation, so it is worth testing properly rather than four times
partially.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from multistack import kube
from multistack.kube import (
    KubeCommandError,
    apply,
    kubectl,
    node_name_for,
    require_cli,
    run_local,
    wait_for,
    wait_for_job,
)

KUBECONFIG = "/tmp/kc.yaml"


class Boom(RuntimeError):
    """Stands in for a capability's own error type."""


# -- run_local ------------------------------------------------------------
def test_stdout_is_returned():
    assert run_local(["echo", "hello"]).strip() == "hello"


def test_stdin_is_closed():
    # The divergence that motivated consolidating: a CLI that reads stdin
    # inherits the terminal otherwise and hangs until the timeout, which
    # reads as a slow cluster rather than a stuck command.
    assert run_local(["cat"], timeout=5) == ""


def test_nonzero_exit_raises_the_callers_error_type():
    with pytest.raises(Boom, match="failed"):
        run_local(["sh", "-c", "echo trouble >&2; exit 1"], error_cls=Boom)


def test_failure_text_falls_back_to_stdout():
    # Some tools report the useful part on the wrong stream, and an empty
    # error message is the worst outcome.
    with pytest.raises(Boom, match="on stdout"):
        run_local(["sh", "-c", "echo on stdout; exit 1"], error_cls=Boom)


def test_empty_output_still_says_something():
    with pytest.raises(Boom, match="no output"):
        run_local(["sh", "-c", "exit 1"], error_cls=Boom)


def test_check_false_returns_output_instead_of_raising():
    assert run_local(["sh", "-c", "echo out; exit 1"], check=False).strip() == "out"


def test_a_timeout_is_the_callers_error_not_subprocess_timeout():
    # MinIO's copy let TimeoutExpired escape, so `except MinIOError` missed
    # timeouts entirely.
    with pytest.raises(Boom, match="timed out after 1s"):
        run_local(["sleep", "3"], timeout=1, error_cls=Boom)


def test_a_missing_binary_is_named():
    with pytest.raises(Boom, match="multistack-does-not-exist"):
        run_local(["multistack-does-not-exist"], error_cls=Boom)


def test_label_replaces_the_argv_prefix_in_messages():
    with pytest.raises(Boom, match="^helm install failed"):
        run_local(["sh", "-c", "exit 1"], error_cls=Boom, label="helm install")


# -- kubectl --------------------------------------------------------------
def test_kubectl_always_scopes_to_the_given_kubeconfig(monkeypatch):
    seen = {}

    def fake(argv, *, timeout, stdin_text=None):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(kube, "_completed", fake)
    kubectl(KUBECONFIG, "get", "nodes")
    assert seen["argv"][:3] == ["kubectl", "--kubeconfig", KUBECONFIG]


def test_kubectl_errors_name_the_operation_not_the_kubeconfig_flag(monkeypatch):
    # "kubectl --kubeconfig /tmp/kc.yaml failed" names the flag; the point
    # of `label` is that it names what was attempted.
    monkeypatch.setattr(
        kube, "_completed",
        lambda argv, *, timeout, stdin_text=None: subprocess.CompletedProcess(
            argv, 1, "", "nope"
        ),
    )
    with pytest.raises(Boom) as exc:
        kubectl(KUBECONFIG, "get", "pods", "-n", "x", error_cls=Boom)
    assert "kubectl get pods -n failed" in str(exc.value)
    assert "--kubeconfig" not in str(exc.value)


# -- apply ----------------------------------------------------------------
@pytest.fixture
def piped(monkeypatch):
    captured = {}

    def fake(argv, *, timeout, stdin_text=None):
        captured["argv"] = argv
        captured["stdin"] = stdin_text
        return subprocess.CompletedProcess(argv, 0, "applied", "")

    monkeypatch.setattr(kube, "_completed", fake)
    return captured


def test_apply_pipes_rather_than_writing_a_temp_file(piped):
    apply(KUBECONFIG, "kind: Namespace")
    assert piped["argv"][-3:] == ["apply", "-f", "-"]
    assert piped["stdin"] == "kind: Namespace"


def test_apply_serialises_a_single_object(piped):
    apply(KUBECONFIG, {"kind": "Namespace"})
    assert json.loads(piped["stdin"]) == {"kind": "Namespace"}


def test_apply_wraps_several_objects_in_a_list(piped):
    # How several objects go in one call without a client library.
    apply(KUBECONFIG, [{"kind": "Service"}, {"kind": "Deployment"}])
    sent = json.loads(piped["stdin"])
    assert sent["kind"] == "List"
    assert [o["kind"] for o in sent["items"]] == ["Service", "Deployment"]


# -- wait_for -------------------------------------------------------------
def test_wait_for_returns_as_soon_as_the_check_passes():
    calls = []
    wait_for(lambda: calls.append(1) or len(calls) >= 2,
             timeout=5, interval=0, description="thing")
    assert len(calls) == 2


def test_wait_for_raises_on_timeout():
    with pytest.raises(Boom, match="thing did not complete within"):
        wait_for(lambda: False, timeout=0, interval=0,
                 description="thing", error_cls=Boom)


def test_wait_for_lets_the_check_fail_fast():
    # A Job that has already failed must not be waited on for another
    # half hour.
    def check():
        raise Boom("already failed")

    with pytest.raises(Boom, match="already failed"):
        wait_for(check, timeout=60, interval=0)


# -- wait_for_job ---------------------------------------------------------
def job_status(monkeypatch, *statuses, logs="the logs"):
    """Feeds `kubectl get job` one status per poll.

    Each status is `succeeded|failed|completions|backoffLimit`, matching
    the jsonpath wait_for_job asks for. Empty fields are absent fields,
    which is how a Job that has not finished reports.
    """
    remaining = list(statuses)

    def fake(argv, *, timeout, stdin_text=None):
        out = logs if "logs" in argv else remaining.pop(0)
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(kube, "_completed", fake)


def test_a_succeeded_job_returns(monkeypatch):
    job_status(monkeypatch, "1|||")
    wait_for_job(KUBECONFIG, "j", "ns", interval=0)


def test_a_running_job_is_waited_on(monkeypatch):
    # Neither count is set while a Job runs, so absence means "still going".
    job_status(monkeypatch, "|||", "|||", "1|||")
    wait_for_job(KUBECONFIG, "j", "ns", interval=0)


def test_a_failed_job_raises_with_its_logs(monkeypatch):
    # By the time anyone looks, the pod may be gone.
    job_status(monkeypatch, "|1||", logs="OOMKilled at layer 3")
    with pytest.raises(Boom) as exc:
        wait_for_job(KUBECONFIG, "j", "ns", interval=0, error_cls=Boom)
    assert "OOMKilled at layer 3" in str(exc.value)


def test_a_job_with_retries_is_detected_when_it_gives_up(monkeypatch):
    """The case string-matching missed. With backoffLimit: 1 a Job that
    exhausts its retries reports failed=2, which matched neither "1" nor
    "/1" — so a failed Job blocked for the full 1800s timeout and its logs
    were never surfaced. full_stack.py's own weight-staging Job sets
    backoffLimit: 1."""
    job_status(monkeypatch, "|1|1|1", "|2|1|1", logs="pip install failed")
    with pytest.raises(Boom, match="pip install failed"):
        wait_for_job(KUBECONFIG, "j", "ns", interval=0, error_cls=Boom)


def test_a_parallel_job_needs_every_completion(monkeypatch):
    # succeeded=1 of completions=3 is not done; string-matching called it
    # finished because the field started with "1".
    job_status(monkeypatch, "1|-|3|0", "3||3|0")
    wait_for_job(KUBECONFIG, "j", "ns", interval=0)


# -- node_name_for --------------------------------------------------------
def node_listing(monkeypatch, text):
    monkeypatch.setattr(
        kube, "_completed",
        lambda argv, *, timeout, stdin_text=None: subprocess.CompletedProcess(
            argv, 0, text, ""
        ),
    )


def test_an_ip_maps_to_its_kubernetes_node_name(monkeypatch):
    # The SDK addresses nodes by IP; Kubernetes by name, and the two
    # coincide only if someone named the hosts that way.
    node_listing(monkeypatch, "rke2-cp01 192.0.2.10\nrke2-wk01 192.0.2.11\n")
    assert node_name_for(KUBECONFIG, "192.0.2.11") == "rke2-wk01"


def test_an_unregistered_ip_says_what_is_registered(monkeypatch):
    node_listing(monkeypatch, "rke2-cp01 192.0.2.10\n")
    with pytest.raises(Boom) as exc:
        node_name_for(KUBECONFIG, "10.0.0.9", error_cls=Boom)
    assert "10.0.0.9" in str(exc.value)
    assert "rke2-cp01" in str(exc.value)


def test_no_nodes_at_all_does_not_produce_an_empty_message(monkeypatch):
    node_listing(monkeypatch, "")
    with pytest.raises(Boom, match="none"):
        node_name_for(KUBECONFIG, "10.0.0.9", error_cls=Boom)


# -- require_cli ----------------------------------------------------------
def test_a_present_binary_returns_its_path():
    assert require_cli("sh").endswith("sh")


def test_a_missing_binary_raises_with_the_purpose():
    with pytest.raises(Boom, match="drives it directly"):
        require_cli("multistack-does-not-exist", error_cls=Boom,
                    purpose="this backend drives it directly")


def test_require_cli_runs_nothing():
    # Deliberately shutil.which: the versions this replaced ran the binary
    # (one via `bash -c` with the name interpolated unquoted), and a
    # presence check that executes can contact a cluster and hang.
    import shutil
    assert require_cli("sh") == shutil.which("sh")


def test_default_error_type_is_the_modules_own():
    with pytest.raises(KubeCommandError):
        require_cli("multistack-does-not-exist")
