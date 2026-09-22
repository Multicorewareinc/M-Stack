"""The accelerator capability: spec, wiring, and the device-plugin driver."""
import json

import pytest

from multistack.accelerator import (
    Accelerator,
    AcceleratorBackend,
    AcceleratorError,
    AcceleratorPrerequisiteError,
)
from multistack.accelerator.drivers import nvidia_device_plugin as ndp
from multistack.accelerator.spec import NODE_FEATURE_LABEL, DevicePluginOptions
from multistack.stack import Stack

KUBECONFIG = "/tmp/kc.yaml"
GPU_NODE = "gpu-01"
PINNED = {"kubernetes.io/hostname": GPU_NODE}


def spec(**kwargs) -> Accelerator:
    return Accelerator(kubeconfig_path=KUBECONFIG, **kwargs)


class _FakeRunner:
    def __init__(self, calls):
        self.calls = calls

    def install_or_upgrade(self, release, **kwargs):
        self.calls.append(("install", release, kwargs))

    def uninstall(self, release, **kwargs):
        self.calls.append(("uninstall", release, kwargs))


def _node(name, labels):
    return {"metadata": {"name": name, "labels": labels}}


def _kubectl_returning(*payloads):
    """A kubectl stub answering successive calls with successive payloads."""
    queue = list(payloads)

    def fake(kubeconfig, *args, **kwargs):
        return json.dumps(queue.pop(0)) if queue else "{}"

    return fake


@pytest.fixture
def driver():
    return ndp.NvidiaDevicePluginDriver()


@pytest.fixture
def calls(driver, monkeypatch):
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda a: _FakeRunner(recorded))
    monkeypatch.setattr(ndp, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(ndp, "require_cluster", lambda *a, **k: None)
    return recorded


def _install(calls):
    return next(c[2] for c in calls if c[0] == "install")


# -- spec -----------------------------------------------------------------


def test_kubeconfig_is_required():
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        Accelerator(kubeconfig_path="")


def test_it_lands_in_kube_system_by_default():
    assert spec().resolved_namespace == "kube-system"


def test_the_resource_name_is_what_a_pod_asks_for():
    assert spec().resource_name == "nvidia.com/gpu"


def test_an_unknown_type_is_refused():
    with pytest.raises(ValueError, match="Unknown accelerator type"):
        spec(type="tenstorrent")


def test_the_chart_version_is_pinned():
    """A floating version is how a changed affinity stops GPUs being
    allocatable with nothing in the release to explain it."""
    assert spec().options.chart_version == "0.20.0"


def test_it_publishes_nothing():
    """Deliberate: it advertises a node property, not an address. See the
    spec's module docstring."""
    assert not getattr(Accelerator, "PROVIDES", {})


def test_the_kubeconfig_is_filled_in_from_the_stack():
    stack = Stack(kubeconfig_path=KUBECONFIG)
    assert stack.build(Accelerator).kubeconfig_path == KUBECONFIG


# -- prerequisites --------------------------------------------------------


def test_an_unlabelled_node_is_refused_before_installing(driver, monkeypatch, calls):
    """The failure this capability exists to prevent: without the label
    the chart's affinity wants, the DaemonSet wants 0 pods and helm
    reports success."""
    monkeypatch.setattr(
        ndp, "kubectl", _kubectl_returning({"items": [_node(GPU_NODE, {})]})
    )
    with pytest.raises(AcceleratorPrerequisiteError, match=NODE_FEATURE_LABEL):
        driver.check_prerequisites(spec(node_selector=PINNED))
    assert not calls, "nothing should have been installed"


def test_the_refusal_names_the_node_and_the_fix(driver, monkeypatch, calls):
    monkeypatch.setattr(
        ndp, "kubectl", _kubectl_returning({"items": [_node(GPU_NODE, {})]})
    )
    with pytest.raises(AcceleratorPrerequisiteError) as caught:
        driver.check_prerequisites(spec(node_selector=PINNED))
    message = str(caught.value)
    assert GPU_NODE in message and "kubectl label node" in message


def test_a_selector_matching_no_node_is_refused(driver, monkeypatch, calls):
    monkeypatch.setattr(ndp, "kubectl", _kubectl_returning({"items": []}))
    with pytest.raises(AcceleratorPrerequisiteError, match="matches no node"):
        driver.check_prerequisites(spec(node_selector=PINNED))


def test_a_labelled_node_passes_without_warnings(driver, monkeypatch, calls):
    monkeypatch.setattr(
        ndp, "kubectl",
        _kubectl_returning({"items": [_node(GPU_NODE, {NODE_FEATURE_LABEL: "true"})]}),
    )
    assert driver.check_prerequisites(spec(node_selector=PINNED)) == []


def test_no_selector_warns_rather_than_refusing(driver, monkeypatch, calls):
    """Legitimate on a cluster where every node has a card, so it is a
    warning -- but it is a pod per node otherwise."""
    warnings = driver.check_prerequisites(spec())
    assert len(warnings) == 1 and "no node_selector" in warnings[0]


# -- create / delete ------------------------------------------------------


def _labelled_then_daemonset(desired):
    return _kubectl_returning(
        {"items": [_node(GPU_NODE, {NODE_FEATURE_LABEL: "true"})]},
        {"items": [{"status": {"desiredNumberScheduled": desired}}]},
    )


def test_create_installs_the_pinned_chart_and_returns_the_resource(
    driver, monkeypatch, calls
):
    monkeypatch.setattr(ndp, "kubectl", _labelled_then_daemonset(1))
    resource = driver.create(spec(node_selector=PINNED))

    assert resource == "nvidia.com/gpu"
    values = _install(calls)
    assert values["chart"] == "nvidia-device-plugin"
    assert values["chart_version"] == "0.20.0"
    assert values["namespace"] == "kube-system"
    assert values["values"]["runtimeClassName"] == "nvidia"
    assert values["values"]["nodeSelector"] == PINNED


def test_a_daemonset_wanting_no_pods_is_an_error(driver, monkeypatch, calls):
    """helm is satisfied -- zero of zero pods are ready -- so this is the
    only place the real outcome is checked."""
    monkeypatch.setattr(ndp, "kubectl", _labelled_then_daemonset(0))
    with pytest.raises(AcceleratorError, match="wants 0 pods"):
        driver.create(spec(node_selector=PINNED))


def test_the_runtime_class_can_be_overridden(driver, monkeypatch, calls):
    monkeypatch.setattr(ndp, "kubectl", _labelled_then_daemonset(1))
    driver.create(spec(
        node_selector=PINNED,
        options=DevicePluginOptions(runtime_class_name="nvidia-experimental"),
    ))
    assert _install(calls)["values"]["runtimeClassName"] == "nvidia-experimental"


def test_an_explicit_empty_selector_differs_from_an_absent_one(driver, monkeypatch, calls):
    """{} overrides the chart's own affinity with "any node"; absent
    leaves it alone. Only one node query happens either way -- an empty
    selector narrows to nothing, so there is no label to pre-check."""
    monkeypatch.setattr(
        ndp, "kubectl",
        _kubectl_returning({"items": [{"status": {"desiredNumberScheduled": 1}}]}),
    )
    driver.create(spec(node_selector={}))
    assert _install(calls)["values"]["nodeSelector"] == {}

    calls.clear()
    monkeypatch.setattr(
        ndp, "kubectl",
        _kubectl_returning({"items": [{"status": {"desiredNumberScheduled": 1}}]}),
    )
    driver.create(spec())
    assert "nodeSelector" not in _install(calls)["values"]


def test_delete_removes_the_release(driver, calls):
    driver.delete(spec(node_selector=PINNED))
    assert calls == [
        ("uninstall", "nvdp",
         {"namespace": "kube-system", "missing_ok": True}),
    ]


# -- registry -------------------------------------------------------------


def test_every_supported_type_has_a_driver():
    assert set(AcceleratorBackend.DRIVERS) == set(Accelerator.SUPPORTED_TYPES)


def test_the_registry_entry_resolves():
    assert AcceleratorBackend.resolve_all()["nvidia_device_plugin"] is (
        ndp.NvidiaDevicePluginDriver
    )
