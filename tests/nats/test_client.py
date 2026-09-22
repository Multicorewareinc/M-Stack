"""NatsAdminClient: the one manual-administration client this platform runs,
deployed and driven through kubectl exec -- never a direct NATS connection."""
import pytest

from multistack.nats.deployment import NatsDeployment
from multistack.nats.deployment import client as client_mod
from multistack.nats.deployment.client import NatsAdminClient
from multistack.nats.deployment.errors import NatsTimeoutError

KC = "/tmp/kc.yaml"


def deployment(**kwargs):
    base = dict(kubeconfig_path=KC, namespace="nats")
    base.update(kwargs)
    return NatsDeployment(**base)


def stub(monkeypatch, *, running=False, wait_succeeds=True):
    calls = {"applied": [], "kubectl": []}

    def fake_apply(kubeconfig_path, manifest, **kw):
        calls["applied"].append(manifest)
        return ""

    def fake_kubectl(kubeconfig_path, *args, **kw):
        calls["kubectl"].append(args)
        if "jsonpath={.status.phase}" in args:
            return "Running" if running else ""
        if "wait" in args:
            if not wait_succeeds:
                raise client_mod.NatsDeploymentError("timed out waiting for condition")
            return ""
        return ""

    monkeypatch.setattr(client_mod, "apply", fake_apply)
    monkeypatch.setattr(client_mod, "kubectl", fake_kubectl)
    return calls


def test_ensure_deploys_the_pod_when_absent(monkeypatch):
    calls = stub(monkeypatch, running=False)
    NatsAdminClient().ensure(deployment())
    assert len(calls["applied"]) == 1
    manifest = calls["applied"][0]
    assert manifest["kind"] == "Pod"
    assert manifest["metadata"]["name"] == "nats-box"
    assert manifest["metadata"]["namespace"] == "nats"
    assert manifest["spec"]["containers"][0]["image"] == "natsio/nats-box:latest"


def test_ensure_is_a_no_op_when_already_running(monkeypatch):
    calls = stub(monkeypatch, running=True)
    NatsAdminClient().ensure(deployment())
    assert calls["applied"] == []


def test_ensure_raises_a_timeout_error_when_the_pod_never_becomes_ready(monkeypatch):
    stub(monkeypatch, running=False, wait_succeeds=False)
    with pytest.raises(NatsTimeoutError):
        NatsAdminClient().ensure(deployment())


def test_exec_targets_the_admin_pod_with_the_in_cluster_server_url(monkeypatch):
    calls = stub(monkeypatch, running=True)
    NatsAdminClient().exec(deployment(), "stream", "ls", "-j")
    exec_call = [c for c in calls["kubectl"] if "exec" in c][0]
    assert exec_call[:4] == ("exec", "-n", "nats", "nats-box")
    assert "--" in exec_call
    assert "nats" in exec_call
    assert "stream" in exec_call and "ls" in exec_call
    assert exec_call[-2:] == ("--server", "nats://nats.nats.svc.cluster.local:4222")


def test_delete_ignores_a_missing_pod(monkeypatch):
    calls = stub(monkeypatch, running=True)
    NatsAdminClient().delete(deployment())
    delete_call = [c for c in calls["kubectl"] if "delete" in c][0]
    assert "--ignore-not-found" in delete_call
