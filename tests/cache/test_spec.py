"""Tests for the Valkey spec, and specifically for what it publishes.

A Valkey is the counter store the rate limiters read, so the value that
matters is the one a consumer connects to. The chart does not name its
Service after the release, and a consumer handed the wrong name fails
DNS resolution at request time rather than failing to wire at build
time -- which is the failure this file exists to prevent.
"""
from __future__ import annotations

from multistack import Cache
from multistack.cache import ValkeyOptions

KUBECONFIG = "/tmp/kc.yaml"


def valkey(**kwargs) -> Cache:
    """A Cache, with chart/values routed into the implementation's options.

    The split is what migrating to the capability shape introduced:
    chart, chart_version and values are Bitnami-chart specifics and live
    on ValkeyOptions, while the spec keeps only what any cache would
    have. Every call below passes them the way it always did."""
    opt_keys = {"chart", "chart_version", "values"}
    opts = {k: kwargs.pop(k) for k in list(kwargs) if k in opt_keys}
    base = dict(kubeconfig_path=KUBECONFIG, name="valkey", namespace="platform")
    base.update(kwargs)
    if opts:
        base["options"] = ValkeyOptions(**opts)
    return Cache(**base)


# -- the Service name follows the chart, not the release ------------------
def test_the_writable_service_is_the_primary():
    # Verified against the live cluster: release "valkey" in platform
    # created valkey-primary and valkey-headless.
    assert valkey().service_name == "valkey-primary"


def test_a_release_name_containing_the_chart_name_is_used_as_is():
    assert valkey(name="valkey-test").service_name == "valkey-test-primary"


def test_a_release_name_that_does_not_is_prefixed():
    """`common.names.fullname` prefixes the chart name when the release
    does not already contain it, so a release called "cache" is served
    by cache-valkey-primary and not cache-primary."""
    assert valkey(name="cache").service_name == "cache-valkey-primary"


def test_fullname_override_wins():
    spec = valkey(name="x", values={"fullnameOverride": "shared-cache"})
    assert spec.service_name == "shared-cache-primary"


def test_name_override_replaces_the_chart_name():
    spec = valkey(name="cache", values={"nameOverride": "kv"})
    assert spec.service_name == "cache-kv-primary"


def test_the_chart_name_comes_from_the_last_segment_of_the_reference():
    # A repository-qualified or OCI reference is still chart "valkey".
    assert valkey(name="cache", chart="bitnami/valkey").service_name == (
        "cache-valkey-primary"
    )


def test_sentinel_collapses_the_services_into_one():
    """With sentinel enabled the chart drops the primary/replica split
    for a single Service that proxies to whichever pod currently holds
    the primary role. Appending -primary there points at nothing."""
    spec = valkey(values={"architecture": "replication",
                          "sentinel": {"enabled": True}})
    assert spec.service_name == "valkey"


def test_replication_without_sentinel_still_writes_to_the_primary():
    spec = valkey(values={"architecture": "replication"})
    assert spec.service_name == "valkey-primary"


# -- the port ------------------------------------------------------------
def test_the_port_defaults_to_the_charts_own():
    assert valkey().port == 6379


def test_a_remapped_primary_port_is_followed():
    spec = valkey(values={"primary": {"service": {"ports": {"valkey": 6380}}}})
    assert spec.port == 6380


def test_sentinel_reads_its_own_port():
    spec = valkey(values={"sentinel": {"enabled": True,
                                       "service": {"ports": {"valkey": 6390}}}})
    assert spec.port == 6390


def test_a_partial_values_tree_does_not_raise():
    """Values are a free-form dict, so half a path is normal input."""
    assert valkey(values={"primary": {"persistence": {"enabled": True}}}).port == 6379
    assert valkey(values={"primary": "not-a-dict"}).port == 6379
    assert valkey(values={"primary": {"service": None}}).port == 6379


# -- what gets published -------------------------------------------------
def test_the_endpoint_is_the_in_cluster_url():
    assert valkey().endpoint == (
        "redis://valkey-primary.platform.svc.cluster.local:6379/0"
    )


def test_the_endpoint_carries_no_credential_even_with_auth_enabled():
    """This value goes into non-secret configuration -- the rate
    limiters put it straight into a ConfigMap. A password in the URL
    would be a password in the ConfigMap."""
    spec = valkey(values={"auth": {"enabled": True,
                                   "existingSecret": "valkey-auth",
                                   "existingSecretPasswordKey": "p"}})
    assert "@" not in spec.endpoint
    assert "valkey-auth" not in spec.endpoint


def test_the_cache_publishes_the_key_policy_reads():
    """Policy.FROM_STACK has read cache_url since it was written, and
    nothing published it. This is the other half."""
    from multistack.policy.spec import Policy

    assert Cache.PROVIDES == {"cache_url": "endpoint"}
    assert Policy.FROM_STACK["cache_url"] == "cache_url"


def test_the_capability_is_registered_so_ordering_can_be_checked():
    from multistack.stack import CAPABILITY_OUTPUT

    assert CAPABILITY_OUTPUT["cache"] == "cache_url"
