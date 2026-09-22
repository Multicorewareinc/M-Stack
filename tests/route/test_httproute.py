"""HTTPRouteDriver: the parent Gateway is created once, the route always."""
import pytest

from multistack.route import Route
from multistack.route.base import RouteError
from multistack.route.drivers import httproute as driver_mod
from multistack.route.drivers.httproute import HTTPRouteDriver

KC = "/tmp/kc.yaml"


def route(**kwargs):
    base = dict(kubeconfig_path=KC, name="mg", namespace="platform",
                service="gateway-model-gateway", port=8080, path_prefix="/v1")
    base.update(kwargs)
    return Route(**base)


def stub(monkeypatch, *, parent_exists=False, crd_exists=True):
    """Records every applied manifest and every kubectl call. `get gateway`
    answers `parent_exists`; `get crd` answers `crd_exists`."""
    calls = {"applied": [], "deleted": []}

    def fake_apply(kubeconfig_path, manifest, **kw):
        calls["applied"].extend(manifest if isinstance(manifest, list) else [manifest])
        return ""

    def fake_kubectl(kubeconfig_path, *args, **kw):
        if "delete" in args:
            i = args.index("delete")
            calls["deleted"].append(tuple(args[i + 1:]))
            return ""
        if "crd" in args:
            return "gateways.gateway.networking.k8s.io" if crd_exists else ""
        if "gateway" in args:
            return "platform-gw" if parent_exists else ""
        return ""

    monkeypatch.setattr(driver_mod, "apply", fake_apply)
    monkeypatch.setattr(driver_mod, "kubectl", fake_kubectl)
    monkeypatch.setattr(driver_mod, "require_cli", lambda *a, **kw: "/usr/bin/kubectl")
    monkeypatch.setattr(driver_mod, "require_cluster", lambda *a, **kw: "v1.30.0")
    return calls


def test_first_call_applies_both_the_parent_and_the_route(monkeypatch):
    calls = stub(monkeypatch, parent_exists=False)
    HTTPRouteDriver().create(route())
    kinds = [m["kind"] for m in calls["applied"]]
    assert kinds == ["Gateway", "HTTPRoute"]


def test_a_second_route_does_not_reapply_an_existing_parent(monkeypatch):
    calls = stub(monkeypatch, parent_exists=True)
    HTTPRouteDriver().create(route())
    kinds = [m["kind"] for m in calls["applied"]]
    assert kinds == ["HTTPRoute"]


def test_route_manifest_carries_the_backend_and_path(monkeypatch):
    calls = stub(monkeypatch, parent_exists=True)
    HTTPRouteDriver().create(route(service="gateway-model-gateway", port=8080, path_prefix="/v1"))
    manifest = calls["applied"][0]
    rule = manifest["spec"]["rules"][0]
    assert rule["matches"][0]["path"] == {"type": "PathPrefix", "value": "/v1"}
    assert rule["backendRefs"] == [{"name": "gateway-model-gateway", "port": 8080}]


def test_parent_binds_to_the_ingress_gateway_service_by_hostname(monkeypatch):
    calls = stub(monkeypatch, parent_exists=False)
    HTTPRouteDriver().create(route())
    parent = next(m for m in calls["applied"] if m["kind"] == "Gateway")
    assert parent["spec"]["addresses"] == [{
        "type": "Hostname",
        "value": "istio-ingressgateway.istio-ingress.svc.cluster.local",
    }]


def test_hostnames_are_carried_onto_the_route_when_given(monkeypatch):
    calls = stub(monkeypatch, parent_exists=True)
    HTTPRouteDriver().create(route(hostnames=["api.example.com"]))
    manifest = calls["applied"][0]
    assert manifest["spec"]["hostnames"] == ["api.example.com"]


def test_no_hostnames_means_no_hostnames_key(monkeypatch):
    calls = stub(monkeypatch, parent_exists=True)
    HTTPRouteDriver().create(route())
    manifest = calls["applied"][0]
    assert "hostnames" not in manifest["spec"]


def test_create_returns_the_route_url(monkeypatch):
    stub(monkeypatch, parent_exists=True)
    endpoint = HTTPRouteDriver().create(route(ingress_address="192.168.6.91"))
    assert endpoint == "http://192.168.6.91/v1"


def test_delete_removes_only_the_route_never_the_parent(monkeypatch):
    calls = stub(monkeypatch)
    HTTPRouteDriver().delete(route())
    assert calls["deleted"] == [("httproute", "mg", "-n", "platform", "--ignore-not-found")]


# -- check_prerequisites() ------------------------------------------------

def test_missing_crd_is_reported_not_raised(monkeypatch):
    stub(monkeypatch, crd_exists=False)
    problems = HTTPRouteDriver().check_prerequisites(route())
    assert any("Gateway API CRDs" in p for p in problems)


def test_crd_present_reports_nothing(monkeypatch):
    stub(monkeypatch, crd_exists=True)
    problems = HTTPRouteDriver().check_prerequisites(route())
    assert problems == []
