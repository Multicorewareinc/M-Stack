"""IngressGatewayBackend: Helm install order, MetalLB CRs, and IP polling.
Nothing shells out — HelmRunner and kube helpers are stubbed, the same way
tests/policy/test_rpm.py and tests/gateway/test_spec.py drive a driver
directly rather than through the cluster-checking CapabilityBackend."""
import pytest

from multistack.ingress_gateway import IngressGateway
from multistack.ingress_gateway.base import IngressGatewayError
from multistack.ingress_gateway.drivers.metallb_istio import MetalLBIstioDriver
from multistack.ingress_gateway.drivers import metallb_istio as driver_mod
from multistack.helm import HelmRunner

KC = "/tmp/kc.yaml"


def gateway(**kwargs):
    base = dict(kubeconfig_path=KC, address_pool=["192.168.1.240-192.168.1.250"])
    base.update(kwargs)
    return IngressGateway(**base)


def stub(monkeypatch, lb_ip="192.168.1.240"):
    """Records every Helm release install/uninstall and every applied
    manifest, and simulates MetalLB assigning `lb_ip` on the first poll."""
    calls = {"install": [], "uninstall": [], "applied": [], "deleted": []}

    def fake_install(self, release, *, chart, namespace, **kw):
        calls["install"].append((release, chart, namespace))

    def fake_uninstall(self, release, *, namespace, **kw):
        calls["uninstall"].append((release, namespace))

    monkeypatch.setattr(HelmRunner, "install_or_upgrade", fake_install)
    monkeypatch.setattr(HelmRunner, "uninstall", fake_uninstall)

    def fake_apply(kubeconfig_path, manifest, **kw):
        calls["applied"].extend(manifest if isinstance(manifest, list) else [manifest])
        return ""

    monkeypatch.setattr(driver_mod, "apply", fake_apply)

    def fake_kubectl(kubeconfig_path, *args, **kw):
        if "delete" in args:
            i = args.index("delete")
            calls["deleted"].append(args[i + 1:i + 3])
            return ""
        if "svc" in args:
            return lb_ip
        return ""

    monkeypatch.setattr(driver_mod, "kubectl", fake_kubectl)
    return calls


# -- create() ------------------------------------------------------------
def test_create_installs_in_dependency_order(monkeypatch):
    calls = stub(monkeypatch)
    MetalLBIstioDriver().create(gateway())
    releases = [c[0] for c in calls["install"]]
    assert releases == ["metallb", "istio-base", "istiod", "istio-ingressgateway"]


def test_pool_and_l2advertisement_use_the_given_addresses(monkeypatch):
    calls = stub(monkeypatch)
    MetalLBIstioDriver().create(gateway(address_pool=["10.0.0.10-10.0.0.20"]))
    pool = next(m for m in calls["applied"] if m["kind"] == "IPAddressPool")
    adv = next(m for m in calls["applied"] if m["kind"] == "L2Advertisement")
    assert pool["spec"]["addresses"] == ["10.0.0.10-10.0.0.20"]
    assert adv["spec"]["ipAddressPools"] == [pool["metadata"]["name"]]
    assert pool["metadata"]["namespace"] == "metallb-system"


def test_create_returns_and_records_the_assigned_ip(monkeypatch):
    stub(monkeypatch, lb_ip="192.168.1.241")
    spec = gateway()
    endpoint = MetalLBIstioDriver().create(spec)
    assert endpoint == "192.168.1.241"
    assert spec.external_endpoint == "192.168.1.241"


def test_no_assigned_ip_times_out(monkeypatch):
    stub(monkeypatch, lb_ip="")
    driver = MetalLBIstioDriver(ready_timeout=-1, poll_interval=0)
    with pytest.raises(IngressGatewayError):
        driver.create(gateway())


# -- delete() --------------------------------------------------------------
def test_delete_tears_down_in_reverse_and_removes_crs(monkeypatch):
    calls = stub(monkeypatch)
    MetalLBIstioDriver().delete(gateway())
    releases = [c[0] for c in calls["uninstall"]]
    assert releases == ["istio-ingressgateway", "istiod", "istio-base", "metallb"]
    kinds = [d[0] for d in calls["deleted"]]
    assert kinds == ["l2advertisement", "ipaddresspool"]
