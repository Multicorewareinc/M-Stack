"""Route: type/options resolution, validation, and the computed url."""
import pytest

from multistack.route import HTTPRouteOptions, Route, VirtualServiceOptions

KC = "/tmp/kc.yaml"


def route(**kwargs):
    base = dict(kubeconfig_path=KC, name="mg", namespace="platform",
                service="gateway-model-gateway", port=8080)
    base.update(kwargs)
    return Route(**base)


def test_defaults_to_httproute_with_matching_options():
    r = route()
    assert r.type == "httproute"
    assert isinstance(r.options, HTTPRouteOptions)


def test_virtualservice_gets_its_own_options():
    r = route(type="virtualservice")
    assert isinstance(r.options, VirtualServiceOptions)


def test_namespace_is_required():
    with pytest.raises(ValueError, match="namespace"):
        Route(kubeconfig_path=KC, name="mg", namespace="",
              service="gateway-model-gateway", port=8080)


def test_path_prefix_must_be_absolute():
    with pytest.raises(ValueError, match="path_prefix"):
        route(path_prefix="v1")


def test_hostname_rejects_a_url():
    with pytest.raises(ValueError, match="hostname"):
        route(hostnames=["http://example.com"])


def test_virtualservice_selector_cannot_be_empty():
    with pytest.raises(ValueError, match="gateway_selector"):
        route(type="virtualservice", options=VirtualServiceOptions(gateway_selector={}))


def test_url_prefers_a_hostname_over_the_ingress_address():
    r = route(path_prefix="/v1", hostnames=["api.example.com"],
              ingress_address="192.168.6.91")
    assert r.url == "http://api.example.com/v1"


def test_url_falls_back_to_the_ingress_address():
    r = route(path_prefix="/v1", ingress_address="192.168.6.91")
    assert r.url == "http://192.168.6.91/v1"


def test_url_is_readable_with_no_address_known_yet():
    r = route(path_prefix="/v1")
    assert r.url == "http://<ingress-address>/v1"


def test_non_default_port_appears_in_the_url():
    r = route(
        path_prefix="/v1", ingress_address="192.168.6.91",
        options=HTTPRouteOptions(listener_port=8443),
    )
    assert r.url == "http://192.168.6.91:8443/v1"


def test_from_stack_pulls_the_ingress_gateway_endpoint():
    assert Route.FROM_STACK["ingress_address"] == "ingress_gateway_endpoint"


def test_provides_route_url():
    assert Route.PROVIDES == {"route_url": "url"}
