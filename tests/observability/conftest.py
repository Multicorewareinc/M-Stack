"""Shared stub for the kube-prometheus-stack driver tests.

`create()` applies the dashboard ConfigMaps (and alert rules) through
`kube.apply` after the Helm install. Stubbed for every test here so no
test can reach a cluster through it, whether or not it asserts on it;
`applied` is the record for the ones that do.
"""
import pytest

from multistack.observability.drivers import kube_prometheus_stack as kps


@pytest.fixture(autouse=True)
def applied(monkeypatch):
    recorded: list = []

    def fake_apply(kubeconfig_path, manifest, *, error_cls=None, **_):
        recorded.extend(manifest if isinstance(manifest, list) else [manifest])

    monkeypatch.setattr(kps, "apply", fake_apply)
    return recorded
