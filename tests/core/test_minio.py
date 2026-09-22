"""Tests for the MinIOTenant spec: validation, topology rules, rendered
Helm values, and the endpoint other components consume."""
import pytest

from multistack import MinIOTenant
from multistack.core.minio import parse_quantity

KUBECONFIG = "/tmp/kc.yaml"


def tenant(**kwargs) -> MinIOTenant:
    defaults = dict(kubeconfig_path=KUBECONFIG, name="minio-test", storage_class="longhorn")
    return MinIOTenant(**{**defaults, **kwargs})


# -- validation -----------------------------------------------------------
def test_kubeconfig_is_required():
    # Never ambient: a stale default kubeconfig targets the wrong cluster.
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        MinIOTenant(kubeconfig_path="", name="x").validate()


def test_distributed_needs_four_drives_for_erasure_coding():
    with pytest.raises(ValueError, match="at least 4"):
        tenant(servers=1, volumes_per_server=2, mode="distributed").validate()
    tenant(servers=2, volumes_per_server=2).validate()


def test_standalone_must_be_a_single_server():
    with pytest.raises(ValueError, match="single server"):
        tenant(mode="standalone", servers=4).validate()
    tenant(mode="standalone", servers=1, volumes_per_server=1).validate()


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match="Invalid mode"):
        tenant(mode="clustered").validate()


def test_bad_volume_size_fails_before_the_operator_is_installed():
    # Otherwise Helm rejects it only after the operator is already there.
    with pytest.raises(ValueError, match="quantity"):
        tenant(volume_size="10 gigs").validate()
    with pytest.raises(ValueError, match="quantity"):
        tenant(volume_size="lots").validate()


def test_credentials_must_be_set_together():
    with pytest.raises(ValueError, match="together"):
        tenant(root_user="admin").validate()


# -- credentials ----------------------------------------------------------
def test_missing_credentials_are_generated_not_defaulted():
    # A hardcoded default like "admin"/"admin12345" ships a tenant whose
    # credentials everyone who read the docs already knows.
    t = tenant()
    assert t.root_user is None and t.root_password is None
    t.ensure_credentials()
    assert len(t.root_user) == 16 and len(t.root_password) == 24


def test_supplied_credentials_are_left_alone():
    t = tenant(root_user="admin", root_password="admin123456")
    t.ensure_credentials()
    assert (t.root_user, t.root_password) == ("admin", "admin123456")


def test_two_tenants_do_not_share_a_generated_password():
    a, b = tenant(), tenant()
    a.ensure_credentials()
    b.ensure_credentials()
    assert a.root_password != b.root_password


# -- endpoint (what other components consume) -----------------------------
def test_endpoint_is_the_in_cluster_s3_url():
    # This is handed to Inference.s3_endpoint_url. The operator names the
    # S3 service `minio` regardless of tenant name.
    assert tenant(namespace="minio-test").endpoint() == (
        "https://minio.minio-test.svc.cluster.local"
    )


def test_endpoint_scheme_follows_auto_cert():
    assert tenant().endpoint().startswith("https://")
    assert tenant(request_auto_cert=False).endpoint().startswith("http://")


# -- rendered values ------------------------------------------------------
def test_pool_carries_the_declared_topology():
    pool = tenant(servers=4, volumes_per_server=2, volume_size="30Gi").helm_values()[
        "tenant"]["pools"][0]
    assert pool["servers"] == 4
    assert pool["volumesPerServer"] == 2
    assert pool["size"] == "30Gi"
    assert pool["storageClassName"] == "longhorn"


def test_storage_class_is_omitted_when_unset():
    # Omitted, not empty-string: an empty storageClassName means "no
    # provisioner" to Kubernetes, not "use the default".
    assert "storageClassName" not in tenant(
        storage_class=None).helm_values()["tenant"]["pools"][0]


def test_extra_values_are_merged():
    assert tenant(extra_values={"foo": "bar"}).helm_values()["foo"] == "bar"


def test_config_secret_name_matches_what_the_operator_creates():
    t = tenant(name="minio-test")
    assert t.config_secret_name == "minio-test-env-configuration"
    assert t.helm_values()["tenant"]["configSecret"]["name"] == t.config_secret_name


def test_label_selector_targets_this_tenant():
    assert tenant(name="minio-test").label_selector == "v1.min.io/tenant=minio-test"


# -- quantity parsing -----------------------------------------------------
@pytest.mark.parametrize("qty,expected", [
    ("10Gi", 10 * 2**30), ("500M", 500 * 10**6), ("1Ti", 2**40), ("100", 100),
])
def test_parse_quantity(qty, expected):
    assert parse_quantity(qty) == expected


def test_parse_quantity_rejects_unknown_units():
    with pytest.raises(ValueError, match="Unknown unit"):
        parse_quantity("10Zi")
