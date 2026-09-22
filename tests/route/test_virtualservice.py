"""VirtualServiceDriver: same two-object split as HTTPRouteDriver, Istio's
own kinds, selector-bound rather than hostname-bound."""
from multistack.route import Route
from multistack.route.drivers import virtualservice as driver_mod
from multistack.route.drivers.virtualservice import VirtualServiceDriver

KC = "/tmp/kc.yaml"


def route(**kwargs):
    base = dict(kubeconfig_path=KC, name="mg", namespace="platform",
                service="gateway-model-gateway", port=8080, path_prefix="/v1",
                type="virtualservice")
    base.update(kwargs)
    return Route(**base)


def stub(monkeypatch, *, parent_exists=False, crd_exists=True):
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
            return "virtualservices.networking.istio.io" if crd_exists else ""
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
    VirtualServiceDriver().create(route())
    kinds = [m["kind"] for m in calls["applied"]]
    assert kinds == ["Gateway", "VirtualService"]


def test_a_second_route_does_not_reapply_an_existing_parent(monkeypatch):
    calls = stub(monkeypatch, parent_exists=True)
    VirtualServiceDriver().create(route())
    kinds = [m["kind"] for m in calls["applied"]]
    assert kinds == ["VirtualService"]


def test_parent_binds_by_label_selector_not_address(monkeypatch):
    calls = stub(monkeypatch, parent_exists=False)
    VirtualServiceDriver().create(route())
    parent = next(m for m in calls["applied"] if m["kind"] == "Gateway")
    assert parent["spec"]["selector"] == {"istio": "ingressgateway"}
    assert "addresses" not in parent["spec"]


def test_route_destination_uses_the_service_fqdn(monkeypatch):
    calls = stub(monkeypatch, parent_exists=True)
    VirtualServiceDriver().create(route())
    vs = calls["applied"][0]
    dest = vs["spec"]["http"][0]["route"][0]["destination"]
    assert dest["host"] == "gateway-model-gateway.platform.svc.cluster.local"
    assert dest["port"]["number"] == 8080


def test_no_hostnames_defaults_to_wildcard(monkeypatch):
    calls = stub(monkeypatch, parent_exists=True)
    VirtualServiceDriver().create(route())
    vs = calls["applied"][0]
    assert vs["spec"]["hosts"] == ["*"]


def test_delete_removes_only_the_route_never_the_parent(monkeypatch):
    calls = stub(monkeypatch)
    VirtualServiceDriver().delete(route())
    assert calls["deleted"] == [("virtualservice", "mg", "-n", "platform", "--ignore-not-found")]


def test_missing_crd_is_reported_not_raised(monkeypatch):
    stub(monkeypatch, crd_exists=False)
    problems = VirtualServiceDriver().check_prerequisites(route())
    assert any("VirtualService" in p for p in problems)
