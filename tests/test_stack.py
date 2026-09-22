"""Tests for Stack: dependency validation at wiring time, and filling in
the values each layer publishes for the next.

The point of this class is that "field required" tells you a field is
missing, not which capability was supposed to produce it — and that
building the layers in the wrong order should cost a line, not a chart
install and a readiness timeout that blames the wrong component.
"""
from __future__ import annotations

import pytest

from multistack import (
    Inference,
    MinIOTenant,
    MissingDependencyError,
    RKE2Cluster,
    RKE2Node,
    Stack,
    Storage,
    Cache,
)

KUBECONFIG = "/tmp/kc.yaml"


@pytest.fixture
def stack():
    return Stack(kubeconfig_path=KUBECONFIG)


# -- the composition root is explicit, never ambient ----------------------
def test_a_stack_requires_a_kubeconfig():
    # The whole distinction from ambient resolution: stated once, at the
    # top, rather than discovered from the environment.
    with pytest.raises(ValueError, match="no ambient fallback"):
        Stack(kubeconfig_path="")


def test_outputs_starts_with_just_the_cluster(stack):
    assert dict(stack.outputs) == {"kubeconfig_path": KUBECONFIG}


def test_outputs_is_a_copy_not_the_live_dict(stack):
    stack.outputs["injected"] = "nope"
    assert "injected" not in stack.outputs


# -- filling in ------------------------------------------------------------
def test_kubeconfig_is_filled_in(stack):
    assert stack.build(Storage, type="longhorn").kubeconfig_path == KUBECONFIG


def test_an_explicit_argument_beats_the_stack(stack):
    built = stack.build(Storage, type="longhorn", kubeconfig_path="/tmp/mine.yaml")
    assert built.kubeconfig_path == "/tmp/mine.yaml"


def test_filling_happens_before_validation(stack):
    """Specs validate at construction, so a spec must be handed a complete
    set of values — not built empty and patched afterwards."""
    built = stack.build(Storage, type="longhorn", replica_count=2)
    built.validate()
    assert built.replica_count == 2


def test_a_spec_still_works_with_no_stack():
    # A Stack is a convenience for composition, never a requirement.
    assert Storage(type="longhorn", kubeconfig_path=KUBECONFIG).storage_class_name


# -- publishing ------------------------------------------------------------
def test_record_publishes_what_a_spec_declares(stack):
    storage = stack.build(Storage, type="longhorn")
    stack.record(storage)
    assert stack.outputs["storage_class"] == "longhorn"


def test_record_calls_a_provider_that_is_a_method(stack):
    # MinIOTenant publishes s3_endpoint_url from endpoint(), a method.
    stack.provide(storage_class="longhorn")
    tenant = stack.build(MinIOTenant, name="minio", namespace="minio")
    stack.record(tenant)
    assert stack.outputs["s3_endpoint_url"].startswith("http")


def test_provide_chains(stack):
    assert stack.provide(a="1").provide(b="2").outputs["a"] == "1"


def test_publishing_none_is_refused(stack):
    """A missing value should stay missing, so the error names the layer
    that owes it rather than surfacing as a null three specs later."""
    with pytest.raises(ValueError, match="refusing to publish"):
        stack.provide(storage_class=None)


# -- ordering, which is the dependency check ------------------------------
def test_building_minio_before_storage_is_refused(stack):
    with pytest.raises(MissingDependencyError) as exc:
        stack.build(MinIOTenant, name="minio")
    message = str(exc.value)
    assert "requires the 'storage' capability" in message
    assert "storage_class" in message
    assert "stack.record" in message, "should say how to fix it"
    assert "kubeconfig_path" in message, "should list what is published"


def test_building_minio_after_recording_storage_works(stack):
    stack.record(stack.build(Storage, type="longhorn"))
    assert stack.build(MinIOTenant, name="minio").storage_class == "longhorn"


def test_the_cluster_layer_requires_nothing(stack):
    # RKE2 produces the kubeconfig; it cannot depend on having one.
    assert RKE2Cluster.REQUIRES == ()
    cluster = stack.build(
        RKE2Cluster, name="c", nodes=[RKE2Node(address="10.0.0.1", role="server")]
    )
    assert cluster.name == "c"


def test_recording_a_valkey_publishes_the_cache_url(stack):
    """The half that was missing. Policy has read cache_url from a Stack
    since it was written, and nothing published it — the error message
    for an unmet one said to use stack.provide() "until there is a cache
    capability"."""
    cache = stack.build(Cache, name="valkey", namespace="platform")
    stack.record(cache)
    assert stack.outputs["cache_url"] == cache.endpoint


def test_policy_takes_its_counter_store_from_the_recorded_valkey(stack):
    from multistack.policy.spec import Policy

    cache = stack.build(Cache, name="valkey", namespace="platform")
    stack.record(cache)

    policy = stack.build(Policy, type="rpm", allow_no_backbone=True)

    assert policy.cache_url == cache.endpoint


def test_the_unmet_cache_url_message_names_the_capability(stack):
    from multistack.policy.spec import Policy

    with pytest.raises(MissingDependencyError) as exc:
        stack.build(Policy, type="rpm", allow_no_backbone=True)

    message = str(exc.value)
    assert "cache_url" in message
    assert "stack.record(valkey)" in message, "should say how to fix it"


def test_valkey_needs_a_cluster_first(stack):
    # Not a cluster provider: it deploys into one, so it cannot be the
    # layer that produces the kubeconfig.
    assert Cache.REQUIRES == ("cluster",)
    assert stack.build(Cache, name="v").kubeconfig_path == KUBECONFIG


def test_inference_wires_the_object_store_through(stack):
    stack.record(stack.build(Storage, type="longhorn"))
    tenant = stack.build(MinIOTenant, name="minio", namespace="minio")
    stack.record(tenant)
    stack.provide(s3_secret_name="minio-creds")

    service = stack.build(Inference, model="s3://models/qwen",
                          served_model_name="qwen")
    assert service.s3_endpoint_url == tenant.endpoint()
    assert service.s3_secret_name == "minio-creds"
    assert service.kubeconfig_path == KUBECONFIG


def test_declared_requirements_match_the_real_dependency_order():
    # Machine-readable, so a composer can order operations and an agent can
    # refuse an out-of-order plan.
    assert RKE2Cluster.REQUIRES == ()
    assert Storage.REQUIRES == ("cluster",)
    assert MinIOTenant.REQUIRES == ("cluster", "storage")
    assert "cluster" in Inference.REQUIRES


def test_the_cluster_spec_takes_the_kubeconfig_path_from_the_stack():
    """RKE2Cluster both takes and publishes kubeconfig_path, which is not
    a contradiction: the path is *where to write* the file, chosen by the
    caller, and afterwards it is where the file is.

    Declaring only PROVIDES left it None under a Stack, so
    _write_kubeconfig_file wrote nothing and every later layer failed with
    "no kubeconfig exists at ...". This is the regression guard.
    """
    stack = Stack(kubeconfig_path=KUBECONFIG)
    cluster = stack.build(
        RKE2Cluster, name="c", nodes=[RKE2Node(address="10.0.0.1", role="server")]
    )
    assert cluster.kubeconfig_path == KUBECONFIG


# -- the wiring declarations, checked for every spec ----------------------
# Three class-level declarations decide whether a Stack can catch a
# wiring mistake at build time. They are plain dicts and tuples, so
# nothing else notices when one is wrong: a REQUIRES naming a capability
# no key maps to is silently never checked, a FROM_STACK field that does
# not exist is silently never filled, and a PROVIDES attribute that is
# missing publishes nothing — which is exactly what the cache did, for a
# cache_url that Policy had been reading from the Stack all along.
#
# docs/dependency-validation-checklist.md carries the same list for a
# human reviewing a new capability.
def every_spec():
    from multistack.ingress_gateway import IngressGateway
    from multistack.policy.spec import Policy

    from multistack import Gateway, Inference

    from multistack.accelerator.spec import Accelerator
    from multistack.billing.spec import Billing
    from multistack.controlplane.spec import ControlPlane
    from multistack.database.spec import Database
    from multistack.enricher.spec import Enricher
    from multistack.observability.spec import Observability
    from multistack.portal.spec import Portal
    from multistack.queue.spec import Queue
    from multistack.route.spec import Route
    from multistack.tokenizer.spec import Tokenizer

    return [RKE2Cluster, Storage, MinIOTenant, Cache, Database, Inference,
            Policy, Gateway, IngressGateway, Observability, Tokenizer,
            ControlPlane, Portal, Accelerator, Enricher, Billing, Route,
            Queue]


@pytest.mark.parametrize("spec_cls", every_spec(), ids=lambda c: c.__name__)
def test_every_required_capability_publishes_something(spec_cls):
    """A REQUIRES naming a capability with no output key is not a
    declaration, it is a comment — `_check_order` skips what it cannot
    look up."""
    from multistack.stack import CAPABILITY_OUTPUT

    for capability in getattr(spec_cls, "REQUIRES", ()):
        assert capability in CAPABILITY_OUTPUT, (
            f"{spec_cls.__name__} requires '{capability}', which no "
            "capability publishes — add it to CAPABILITY_OUTPUT"
        )


@pytest.mark.parametrize("spec_cls", every_spec(), ids=lambda c: c.__name__)
def test_every_wired_field_exists_on_the_spec(spec_cls):
    for field in getattr(spec_cls, "FROM_STACK", {}):
        assert field in spec_cls.model_fields, (
            f"{spec_cls.__name__}.FROM_STACK wires '{field}', which is not "
            "a field on it — it would never be filled in"
        )


@pytest.mark.parametrize("spec_cls", every_spec(), ids=lambda c: c.__name__)
def test_every_published_attribute_exists(spec_cls):
    for key, attribute in getattr(spec_cls, "PROVIDES", {}).items():
        assert hasattr(spec_cls, attribute) or attribute in spec_cls.model_fields, (
            f"{spec_cls.__name__}.PROVIDES publishes '{key}' from "
            f"'{attribute}', which it does not have — record() would "
            "silently publish nothing"
        )


@pytest.mark.parametrize("spec_cls", every_spec(), ids=lambda c: c.__name__)
def test_every_published_key_is_a_capability_output(spec_cls):
    """So a dependent's REQUIRES can be satisfied by recording this.

    A key published under a name nothing maps to still fills a
    FROM_STACK, but `_check_order` cannot see it, so building in the
    wrong order stops being an error.
    """
    from multistack.stack import CAPABILITY_OUTPUT

    published = set(CAPABILITY_OUTPUT.values())
    for key in getattr(spec_cls, "PROVIDES", {}):
        assert key in published, (
            f"{spec_cls.__name__} publishes '{key}', which is not any "
            "capability's output — add it to CAPABILITY_OUTPUT"
        )


# -- the database capability ----------------------------------------------
def test_recording_a_cnpg_cluster_publishes_the_database_url(stack):
    from multistack.database import Database, DatabaseConfig

    cluster = stack.build(
        Database, name="admin-pg", namespace="platform-db",
        database=DatabaseConfig(name="admin_control_plane", owner="admin",
                                password="s3cret"),
    )
    stack.record(cluster)

    assert stack.outputs["database_url"] == cluster.endpoint
    assert "s3cret" not in stack.outputs["database_url"]


def test_an_operator_only_cnpg_spec_publishes_nothing(stack):
    """record() skips a None, so installing the operator does not
    announce a database that does not exist yet."""
    from multistack.database import Database

    stack.record(stack.build(Database))
    assert "database_url" not in stack.outputs


def test_the_unmet_database_url_message_names_the_capability(stack):
    from multistack.stack import CAPABILITY_OUTPUT, Stack

    assert CAPABILITY_OUTPUT["database"] == "database_url"
    assert "create_cluster()" in Stack(kubeconfig_path=KUBECONFIG)._unmet_message(
        type("Fake", (), {"__name__": "Fake"}), [("database_url", "database_url")])
