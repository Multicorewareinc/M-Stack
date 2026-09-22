"""The portal capability: spec, wiring, and the nginx SPA driver.

Most of these are about one failure mode. A portal whose proxy is
misconfigured does not return an error -- it returns 200 with the SPA's
own index.html, and the browser reports a JSON parse error somewhere
unrelated. Every guard here exists to turn one of those into a loud
failure.
"""
import pytest

from multistack.portal import Portal, PortalPrerequisiteError
from multistack.portal.drivers import nginx_spa as ng
from multistack.portal.spec import AdminPortalOptions, DEFAULT_API_PREFIXES
from multistack.stack import CAPABILITY_OUTPUT, Stack

KUBECONFIG = "/tmp/kc.yaml"
UPSTREAM = "http://admin-control-plane.platform.svc.cluster.local:8000"


def spec(**kwargs) -> Portal:
    kwargs.setdefault("api_upstream", UPSTREAM)
    return Portal(kubeconfig_path=KUBECONFIG, **kwargs)


class _FakeRunner:
    def __init__(self, calls):
        self.calls = calls

    def install_or_upgrade(self, release, **kwargs):
        self.calls.append(("install", release, kwargs))

    def uninstall(self, release, **kwargs):
        self.calls.append(("uninstall", release, kwargs))


@pytest.fixture
def driver():
    return ng.NginxSPADriver()


@pytest.fixture
def calls(driver, monkeypatch):
    recorded = []
    monkeypatch.setattr(driver, "_helm", lambda p: _FakeRunner(recorded))
    monkeypatch.setattr(ng, "require_cli", lambda *a, **k: None)
    monkeypatch.setattr(ng, "require_cluster", lambda *a, **k: None)
    return recorded


def _install(calls):
    return next(c[2] for c in calls if c[0] == "install")


# -- spec -----------------------------------------------------------------


def test_an_empty_upstream_is_refused_by_default():
    """The silent failure this prevents: with no upstream every API call
    falls through to the SPA and returns HTML with a 200."""
    with pytest.raises(ValueError, match="api_upstream is empty"):
        Portal(kubeconfig_path=KUBECONFIG).validate()


def test_an_empty_upstream_is_allowed_when_asked_for_deliberately():
    Portal(kubeconfig_path=KUBECONFIG, allow_no_api=True).validate()


def test_the_resolver_must_be_an_address_not_a_name():
    """nginx resolves its own resolver at startup; `nginx -t` fails with
    'host not found in resolver' if this is a Service name."""
    with pytest.raises(ValueError, match="must be an IP address"):
        spec(resolver="kube-dns.kube-system.svc.cluster.local")


def test_a_real_resolver_address_is_accepted():
    assert spec(resolver="10.43.0.10").resolver == "10.43.0.10"


def test_an_api_prefix_must_be_a_path():
    with pytest.raises(ValueError, match="must start with '/'"):
        spec(api_prefixes=["v1/"])


def test_both_default_prefixes_are_present():
    """/v1/ alone is not enough: the client calls /api/auth/refresh
    directly, and a refresh that returns the SPA logs the user out with
    no error anywhere."""
    assert set(DEFAULT_API_PREFIXES) == {"/v1/", "/api/"}
    assert set(spec().api_prefixes) == {"/v1/", "/api/"}


def test_both_portals_share_one_chart_with_different_images():
    admin, org = spec(type="admin"), spec(type="organization")
    assert admin.options.chart == org.options.chart == "ui/chart"
    assert admin.options.image_repository != org.options.image_repository


def test_portals_default_to_the_frontend_namespace():
    assert spec().resolved_namespace == "frontend"


def test_the_endpoint_names_the_release():
    assert spec(type="organization").endpoint == (
        "http://organization-portal.frontend.svc.cluster.local:80"
    )


# -- wiring ---------------------------------------------------------------


def test_it_requires_only_a_cluster():
    # A portal can be deployed before its API exists, deliberately.
    assert Portal.REQUIRES == ("cluster",)


def test_the_upstream_is_filled_from_a_recorded_control_plane():
    stack = Stack(KUBECONFIG)
    stack.provide(controlplane_endpoint=UPSTREAM)
    built = stack.build(Portal)
    assert built.api_upstream == UPSTREAM


def test_it_publishes_a_portal_endpoint():
    assert Portal.PROVIDES == {"portal_endpoint": "endpoint"}
    assert CAPABILITY_OUTPUT["portal"] == "portal_endpoint"


def test_recording_it_publishes_the_endpoint():
    stack = Stack(KUBECONFIG)
    stack.record(spec())
    assert stack._values["portal_endpoint"] == spec().endpoint


# -- driver ---------------------------------------------------------------


def test_create_installs_the_shared_chart(driver, calls):
    assert driver.create(spec()) == spec().endpoint
    _, release, kwargs = next(c for c in calls if c[0] == "install")
    assert release == "admin-portal"
    assert kwargs["chart"] == "ui/chart"
    assert kwargs["namespace"] == "frontend"


def test_create_passes_the_proxy_configuration(driver, calls):
    driver.create(spec())
    config = _install(calls)["values"]["config"]
    assert config["apiUpstream"] == UPSTREAM
    assert config["apiPrefixes"] == ["/v1/", "/api/"]
    assert config["resolver"] == "10.43.0.10"


def test_each_type_ships_its_own_image(driver, calls):
    driver.create(spec(type="organization"))
    repo = _install(calls)["values"]["image"]["repository"]
    assert repo.endswith("organization-portal")


def test_a_node_selector_reaches_the_chart(driver, calls):
    """The image is built locally and sideloaded into one node's
    containerd, so a pod scheduled elsewhere is ImagePullBackOff."""
    driver.create(spec(node_selector={"kubernetes.io/hostname": "rke2-wrk-2"}))
    assert _install(calls)["values"]["nodeSelector"] == {
        "kubernetes.io/hostname": "rke2-wrk-2"
    }


def test_no_node_selector_means_the_key_is_absent(driver, calls):
    # Absent leaves the chart's own default in place; an explicit {} would
    # override it with "schedule anywhere".
    driver.create(spec())
    assert "nodeSelector" not in _install(calls)["values"]


def test_an_empty_node_selector_is_sent_as_a_deliberate_override(driver, calls):
    driver.create(spec(node_selector={}))
    assert _install(calls)["values"]["nodeSelector"] == {}


def test_dropping_the_api_prefix_warns(driver, calls):
    warnings = driver.check_prerequisites(spec(api_prefixes=["/v1/"]))
    assert any("/api/auth/refresh" in w for w in warnings)


def test_no_upstream_with_allow_no_api_warns(driver, calls):
    p = Portal(kubeconfig_path=KUBECONFIG, allow_no_api=True,
               node_selector={"kubernetes.io/hostname": "rke2-wrk-2"})
    warnings = driver.check_prerequisites(p)
    assert any("JSON parse error" in w for w in warnings)


def test_no_node_selector_warns_about_the_sideloaded_image(driver, calls):
    warnings = driver.check_prerequisites(spec())
    assert any("ImagePullBackOff" in w for w in warnings)


def test_a_fully_configured_portal_warns_about_nothing(driver, calls):
    p = spec(node_selector={"kubernetes.io/hostname": "rke2-wrk-2"})
    assert driver.check_prerequisites(p) == []


def test_delete_uninstalls_and_tolerates_absence(driver, calls):
    driver.delete(spec())
    assert next(c[2] for c in calls if c[0] == "uninstall")["missing_ok"] is True
