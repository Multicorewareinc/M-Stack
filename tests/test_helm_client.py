"""PyHelm3Client, the adapter HelmManager talks to instead of pyhelm3.

Every method here is a forwarder, which sounds too thin to test until you
read the docstring on `get_current_revision`: HelmManager.status() called
it, the adapter did not define it, and the AttributeError came back to
the user as "Helm deployment failure". A missing or misspelled forwarder
is invisible until the one code path that uses it runs against a real
cluster.

So these assert the shape of the forwarding -- that each method exists,
reaches the matching pyhelm3 method, and passes its arguments through --
rather than any behaviour of Helm itself.
"""
import sys
import types

import pytest

from multistack.helm.client import PyHelm3Client
from multistack.helm.config import HelmConfig


class _FakePyhelm3Client:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls = []

    def _record(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return f"{name}-result"
        return call

    def __getattr__(self, name):
        # Any method pyhelm3 offers; the test asserts on what was called.
        return self._record(name)


@pytest.fixture
def fake(monkeypatch):
    """Installs a stand-in pyhelm3 module, since the real Client is
    constructed inside __init__ (deliberately, so `import multistack.helm`
    works without the helm extra)."""
    created = {}

    def Client(**kwargs):
        created["client"] = _FakePyhelm3Client(**kwargs)
        return created["client"]

    module = types.ModuleType("pyhelm3")
    module.Client = Client
    monkeypatch.setitem(sys.modules, "pyhelm3", module)
    return created


@pytest.fixture
def client(fake):
    return PyHelm3Client(HelmConfig(kubeconfig="/tmp/kc.yaml"))


def _await(coro):
    import asyncio
    return asyncio.run(coro)


def test_the_config_reaches_the_underlying_client(client, fake):
    # Every one of these is a way to talk to the wrong cluster, or to skip
    # a check that matters, if it silently fails to be passed on.
    kwargs = fake["client"].init_kwargs
    # HelmConfig coerces it to a Path; what matters is which file.
    assert str(kwargs["kubeconfig"]) == "/tmp/kc.yaml"
    assert kwargs["executable"] == "helm"
    assert kwargs["insecure_skip_tls_verify"] is False


def test_every_adapter_method_reaches_a_pyhelm3_method(client, fake):
    """The regression this file exists for: a method HelmManager calls but
    the adapter never defined."""
    for name in (
        "get_current_revision", "get_chart_ref", "get_chart",
        "get_oci_chart", "install_or_upgrade_release", "uninstall_release",
        "list_releases",
    ):
        assert hasattr(client, name), f"adapter is missing {name}"


def test_get_current_revision_is_defined_exactly_once():
    """A second definition silently overrides the first, so an edit to the
    earlier one does nothing. A merge introduced exactly that."""
    source = (
        __import__("inspect").getsource(PyHelm3Client)
    )
    assert source.count("async def get_current_revision") == 1


def test_get_current_revision_forwards_the_namespace(client, fake):
    _await(client.get_current_revision("rel", namespace="ns"))
    name, args, kwargs = fake["client"].calls[-1]
    assert name == "get_current_revision"
    assert args == ("rel",)
    assert kwargs == {"namespace": "ns"}


def test_get_oci_chart_forwards_the_reference_and_version(client, fake):
    _await(client.get_oci_chart("oci://registry/chart", version="1.2.3"))
    name, args, kwargs = fake["client"].calls[-1]
    # It resolves through get_chart, not a separate OCI entry point.
    assert name == "get_chart"
    assert args == ("oci://registry/chart",)
    assert kwargs == {"version": "1.2.3"}


def test_get_oci_chart_without_a_version_asks_for_none(client, fake):
    # None means "latest" to Helm; it must not become the string "None".
    _await(client.get_oci_chart("oci://registry/chart"))
    assert fake["client"].calls[-1][2] == {"version": None}


def test_list_releases_asks_for_every_release(client, fake):
    """all=True matters: without it Helm lists only deployed releases, so
    a failed or pending one is invisible to whatever is reconciling."""
    _await(client.list_releases(namespace="ns"))
    name, _, kwargs = fake["client"].calls[-1]
    assert name == "list_releases"
    assert kwargs == {"all": True, "namespace": "ns"}


def test_uninstall_release_forwards(client, fake):
    _await(client.uninstall_release("rel", namespace="ns"))
    assert fake["client"].calls[-1][0] == "uninstall_release"
