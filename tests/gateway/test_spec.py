"""The gateway spec, and the two rules worth being strict about."""
import pytest

from multistack import Gateway, Policy
from multistack.gateway.drivers.modelgateway import ModelGatewayDriver

KC = "/tmp/kc.yaml"


def gateway(**kwargs):
    base = dict(kubeconfig_path=KC, upstream_url="http://vllm:8000",
                api_key_secret="gateway-keys",
                org_cp_internal_url="http://organization-control-plane:8000")
    base.update(kwargs)
    return Gateway(**base)


def test_a_policy_chain_needs_a_workable_timeout():
    """Measured on a live cluster: a /check across a pod hop plus the
    limiter's Redis round-trip took 62ms cold and 32ms warm. At the
    service's own default of 50ms the first request of every idle period
    exceeded it and failed closed with a 503."""
    with pytest.raises(ValueError) as raised:
        gateway(policy_endpoints=["http://rpm:8000/check"], policy_timeout_ms=50)
    assert "62ms" in str(raised.value)

    # Without a chain the timeout is unused, so it is not policed.
    assert gateway(policy_timeout_ms=50).policy_timeout_ms == 50


def test_a_route_cannot_carry_a_provider_credential():
    """MODEL_ROUTES accepts a nested object with that provider's api_key.
    A credential cannot live in a spec, so the typed field takes URLs
    only and the error says where the real document goes."""
    with pytest.raises(ValueError) as raised:
        gateway(model_routes={"claude": {"url": "http://x", "api_key": "sk-1"}})
    assert "api_key_secret" in str(raised.value)


def test_routes_must_be_http_urls():
    with pytest.raises(ValueError):
        gateway(model_routes={"m": "vllm:8000"})


def test_the_key_secret_is_required_and_is_a_name():
    with pytest.raises(ValueError):
        Gateway(kubeconfig_path=KC, upstream_url="http://vllm:8000")


def test_fail_mode_is_closed_by_default_and_checked():
    assert gateway().policy_fail_mode == "closed"
    with pytest.raises(ValueError):
        gateway(policy_fail_mode="maybe")


def test_defaults_deploy_a_plain_authenticated_proxy():
    """ADR-006: features attach by config. Both off means the gateway
    builds no policy client and no publisher."""
    plain = gateway()
    assert plain.policy_endpoints == []
    assert plain.event_backbone_url == ""


def test_a_policy_endpoint_comes_from_the_policy_spec():
    limiter = Policy(kubeconfig_path=KC, cache_url="redis://v:6379/0",
                     event_backbone_url="nats://n:4222")
    wired = gateway(policy_endpoints=[limiter.endpoint])
    assert wired.policy_endpoints == [
        "http://rpm-rate-limiter-rpm.policy.svc:8000/check"]


def test_routes_are_rendered_as_a_json_string():
    # The service parses MODEL_ROUTES with json.loads, so a dict handed
    # straight through would arrive as Python's repr and fail to parse.
    values = ModelGatewayDriver()._values(
        gateway(model_routes={"qwen": "http://vllm:8000"}))
    assert values["config"]["modelRoutes"] == '{"qwen": "http://vllm:8000"}'


def test_the_secret_is_referenced_never_inlined():
    values = ModelGatewayDriver()._values(gateway())
    assert values["secret"] == {"existingSecret": "gateway-keys"}


# -- scheduling -----------------------------------------------------------
#
# No in-cluster registry: the gateway image exists only on the node it was
# imported to, so a pod scheduled elsewhere stays ImagePullBackOff.


def test_the_gateway_driver_forwards_a_node_selector():
    from multistack import Gateway
    from multistack.gateway.drivers.modelgateway import ModelGatewayDriver

    pin = {"kubernetes.io/hostname": "rke2-wrk-2"}
    values = ModelGatewayDriver()._values(Gateway(
        kubeconfig_path="/tmp/kc.yaml", upstream_url="http://u",
        api_key_secret="s", org_cp_internal_url="http://org-cp:8000",
        node_selector=pin))
    assert values["nodeSelector"] == pin


def test_the_gateway_driver_omits_the_key_when_unset():
    from multistack import Gateway
    from multistack.gateway.drivers.modelgateway import ModelGatewayDriver

    values = ModelGatewayDriver()._values(Gateway(
        kubeconfig_path="/tmp/kc.yaml", upstream_url="http://u",
        api_key_secret="s", org_cp_internal_url="http://org-cp:8000"))
    assert "nodeSelector" not in values
