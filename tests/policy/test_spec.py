"""The policy spec: what it refuses, and why each refusal exists.

Every case here is a failure that is invisible at deploy time — the pod
comes up Ready and answers /check while limiting nothing — which is why
they are construction-time errors rather than warnings.
"""
import pytest

from multistack import Policy, RateLimits

KC = "/tmp/kc.yaml"
CACHE = "redis://valkey.policy.svc:6379/0"
NATS = "nats://nats.policy.svc:4222"


def policy(**kwargs):
    base = dict(kubeconfig_path=KC, cache_url=CACHE, event_backbone_url=NATS)
    base.update(kwargs)
    return Policy(**base)


def test_an_empty_backbone_is_refused():
    """Counting is asynchronous: /check reads counters a consumer fills
    from the gateway's events. With no backbone nothing advances and
    every request is allowed, by a service that looks healthy."""
    with pytest.raises(ValueError) as raised:
        Policy(kubeconfig_path=KC, cache_url=CACHE)
    assert "nothing would count" in str(raised.value)
    assert "allow_no_backbone" in str(raised.value)


def test_an_empty_backbone_can_be_chosen_deliberately():
    # The case that matters while wiring a gateway up before NATS exists.
    chosen = Policy(kubeconfig_path=KC, cache_url=CACHE, allow_no_backbone=True)
    assert chosen.event_backbone_url == ""


def test_the_counter_store_has_no_default():
    """The service defaults VALKEY_URL to localhost, which inside a pod is
    no Redis at all — and it fails open, so nothing is limited and
    nothing says so."""
    with pytest.raises(ValueError):
        Policy(kubeconfig_path=KC, event_backbone_url=NATS)


def test_a_user_model_key_must_name_both_scopes():
    with pytest.raises(ValueError) as raised:
        policy(limits={"user_model_overrides": {"vip-key": 300}})
    assert "<principal>|<model>" in str(raised.value)
    # And the pair form is accepted.
    assert policy(limits={"user_model_overrides": {"vip-key|gpt-4o": 300}})


def test_limits_are_non_negative_integers():
    for bad in ({"model_overrides": {"m": -1}}, {"model_overrides": {"m": "many"}}):
        with pytest.raises(ValueError):
            policy(limits=bad)


def test_zero_means_unlimited_not_blocked():
    """Worth asserting because it is the opposite of what most people
    assume, and a limits document of all zeros deploys a service that
    denies nothing."""
    assert RateLimits().user_default == 0
    assert policy(limits={}).limits.model_default == 0


def test_dedupe_ttl_must_cover_the_window_it_protects():
    with pytest.raises(ValueError):
        policy(options={"dedupe_ttl": 30})


def test_the_endpoint_follows_the_release_name():
    assert policy().endpoint == (
        "http://rpm-rate-limiter-rpm.policy.svc:8000/check")
    renamed = policy(options={"release_name": "limits"})
    assert renamed.endpoint.startswith("http://limits-rate-limiter-rpm.")


def test_assignment_is_revalidated():
    live = policy()
    with pytest.raises(ValueError):
        live.event_backbone_url = ""


def test_unknown_fields_are_refused():
    with pytest.raises(ValueError):
        policy(rate_limits={"user_default": 5})     # the env var's name


# -- tpm ------------------------------------------------------------------
def test_tpm_is_a_supported_type_with_its_own_options():
    from multistack.policy.spec import TPMOptions

    spec = policy(type="tpm")
    assert isinstance(spec.options, TPMOptions)
    assert spec.resolved_namespace == "policy"


def test_the_service_name_follows_the_type_not_rpm():
    """`endpoint` is built from `service_name`, and a wrong name points the
    gateway's policy chain at a Service that does not exist — which fails
    open or closed depending on POLICY_FAIL_MODE, and neither is
    intended."""
    assert policy(type="rpm").service_name == "rpm-rate-limiter-rpm"
    assert policy(type="tpm").service_name == "tpm-rate-limiter-tpm"
    assert policy(type="tpm").endpoint == (
        "http://tpm-rate-limiter-tpm.policy.svc:8000/check")


def test_a_release_already_naming_the_chart_is_not_doubled():
    """The chart's fullname template collapses `<release>-<chart>` when the
    release already contains the chart name. Mirrored here, or the endpoint
    names a Service that was never created."""
    spec = policy(type="tpm", options={"release_name": "rate-limiter-tpm"})
    assert spec.service_name == "rate-limiter-tpm"


def test_tpm_refuses_rpms_durable_name():
    """Both consumers read the same stream and subject; the durable is the
    only thing giving each its own cursor. Sharing one splits the stream,
    so each sees about half the events and both under-count silently."""
    with pytest.raises(ValueError, match="RPM limiter's durable"):
        policy(type="tpm", options={"durable_name": "rpm-counter"})


def test_the_backbone_rule_applies_to_tpm_too():
    with pytest.raises(ValueError, match="nothing would count"):
        policy(type="tpm", event_backbone_url="")


def test_an_unknown_policy_type_is_refused():
    with pytest.raises(ValueError, match="quota"):
        policy(type="quota")


# -- scheduling -----------------------------------------------------------


def _policy(**kwargs):
    from multistack import Policy
    from multistack.policy.spec import RateLimits
    kwargs.setdefault("type", "rpm")
    return Policy(kubeconfig_path="/tmp/kc.yaml",
                  cache_url="redis://c:6379/0", allow_no_backbone=True,
                  limits=RateLimits(), **kwargs)


def test_both_policy_drivers_forward_a_node_selector():
    from multistack.policy.drivers.rpm import RPMDriver
    from multistack.policy.drivers.tpm import TPMDriver

    pin = {"kubernetes.io/hostname": "rke2-wrk-2"}
    assert RPMDriver()._values(_policy(node_selector=pin))["nodeSelector"] == pin
    assert TPMDriver()._values(
        _policy(type="tpm", node_selector=pin))["nodeSelector"] == pin


def test_a_policy_without_a_node_selector_omits_the_key():
    from multistack.policy.drivers.rpm import RPMDriver

    assert "nodeSelector" not in RPMDriver()._values(_policy())


# -- cache_auth_url ------------------------------------------------------
# The counter store's password has to reach the pod without passing
# through a ConfigMap, where `get configmaps` in the namespace would read
# it. These cover the split: cache_url stays credential-free for the
# ConfigMap, cache_auth_url carries the password into the Secret.
def test_cache_auth_url_defaults_to_none_so_an_unauthenticated_cache_works():
    policy = Policy(kubeconfig_path=KC, cache_url=CACHE,
                    allow_no_backbone=True)

    assert policy.cache_auth_url is None


def test_cache_auth_url_is_masked_in_repr_and_dump():
    """A spec reaching a traceback or a bare print() must not show it."""
    policy = Policy(kubeconfig_path=KC, cache_url=CACHE,
                    cache_auth_url="redis://:s3cret@valkey:6379/0",
                    allow_no_backbone=True)

    assert "s3cret" not in repr(policy)
    assert "s3cret" not in str(policy.model_dump())
    # And is still readable where it is meant to be.
    assert policy.cache_auth_url.get_secret_value() == \
        "redis://:s3cret@valkey:6379/0"


def test_cache_url_is_left_credential_free_alongside_it():
    """The two fields are not alternatives -- both render, to different
    places. Collapsing them into one would put the password in the
    ConfigMap, which is the bug this pair exists to prevent."""
    policy = Policy(kubeconfig_path=KC, cache_url=CACHE,
                    cache_auth_url="redis://:s3cret@valkey:6379/0",
                    allow_no_backbone=True)

    assert policy.cache_url == CACHE
    assert "s3cret" not in policy.cache_url
