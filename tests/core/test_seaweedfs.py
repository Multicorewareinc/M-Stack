"""Tests for the SeaweedFSCluster spec: validation, the rendered Helm
values, and the endpoint other components consume."""
import pytest

from multistack import SeaweedFSCluster
from multistack.core.seaweedfs import chart_fullname, parse_quantity

KUBECONFIG = "/tmp/kc.yaml"


def cluster(**kwargs) -> SeaweedFSCluster:
    defaults = dict(
        kubeconfig_path=KUBECONFIG, name="seaweedfs-test", storage_class="longhorn",
    )
    return SeaweedFSCluster(**{**defaults, **kwargs})


# -- validation -------------------------------------------------------------
def test_kubeconfig_is_required():
    # Never ambient: a stale default kubeconfig targets the wrong cluster.
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        SeaweedFSCluster(kubeconfig_path="", name="x").validate()


@pytest.mark.parametrize("field", ["master_replicas", "filer_replicas", "volume_replicas"])
def test_replica_counts_must_be_at_least_one(field):
    with pytest.raises(ValueError, match="must be at least 1"):
        cluster(**{field: 0}).validate()


def test_invalid_replication_code_is_rejected():
    with pytest.raises(ValueError, match="Invalid replication code"):
        cluster(replication="7").validate()
    with pytest.raises(ValueError, match="Invalid replication code"):
        cluster(replication="abc").validate()
    cluster(replication="001").validate()


def test_bad_volume_size_fails_before_the_release_is_installed():
    # Otherwise Helm rejects it only after the release is already there.
    with pytest.raises(ValueError, match="quantity"):
        cluster(volume_size="10 gigs").validate()
    with pytest.raises(ValueError, match="quantity"):
        cluster(volume_size="lots").validate()


def test_credentials_must_be_set_together():
    with pytest.raises(ValueError, match="together"):
        cluster(access_key="admin").validate()


# -- credentials --------------------------------------------------------------
def test_missing_credentials_are_generated_not_defaulted():
    # A hardcoded default like "admin"/"admin12345" ships a cluster whose
    # credentials everyone who read the docs already knows.
    c = cluster()
    assert c.access_key is None and c.secret_key is None
    c.ensure_credentials()
    assert len(c.access_key) == 16 and len(c.secret_key) == 24


def test_supplied_credentials_are_left_alone():
    c = cluster(access_key="admin", secret_key="admin123456")
    c.ensure_credentials()
    assert (c.access_key, c.secret_key) == ("admin", "admin123456")


def test_two_clusters_do_not_share_a_generated_secret_key():
    a, b = cluster(), cluster()
    a.ensure_credentials()
    b.ensure_credentials()
    assert a.secret_key != b.secret_key


def test_s3_config_json_carries_the_credentials():
    c = cluster(access_key="admin", secret_key="admin123456", name="models")
    import json
    config = json.loads(c.s3_config_json())
    identity = config["identities"][0]
    assert identity["name"] == "models"
    assert identity["credentials"][0] == {"accessKey": "admin", "secretKey": "admin123456"}
    assert set(identity["actions"]) == {"Admin", "Read", "Write"}


# -- chart fullname / service naming (mirrors the chart's own helper) --------
def test_fullname_is_the_release_name_when_it_already_names_the_chart():
    assert chart_fullname("seaweedfs") == "seaweedfs"
    assert chart_fullname("my-seaweedfs-cluster") == "my-seaweedfs-cluster"


def test_fullname_appends_the_chart_name_otherwise():
    assert chart_fullname("models") == "models-seaweedfs"


def test_s3_service_name_follows_fullname():
    assert cluster(name="seaweedfs-test").s3_service_name == "seaweedfs-test-s3"
    assert cluster(name="models").s3_service_name == "models-seaweedfs-s3"


# -- endpoint (what other components consume) --------------------------------
def test_endpoint_is_the_in_cluster_s3_url():
    # This is handed to Inference.s3_endpoint_url.
    assert cluster(name="seaweedfs-test", namespace="objectstore").endpoint() == (
        "http://seaweedfs-test-s3.objectstore.svc.cluster.local:8333"
    )


def test_endpoint_uses_the_configured_s3_port():
    assert cluster(s3_port=9333).endpoint().endswith(":9333")


def test_endpoint_reflects_a_custom_release_name():
    c = cluster(name="models", release_name="mystack")
    assert c.endpoint() == "http://mystack-seaweedfs-s3.seaweedfs.svc.cluster.local:8333"


# -- rendered values ----------------------------------------------------------
def test_dataDir_carries_the_declared_topology():
    values = cluster(volume_replicas=3, volume_size="30Gi").helm_values()
    assert values["volume"]["replicas"] == 3
    data_dir = values["volume"]["dataDirs"][0]
    assert data_dir["type"] == "persistentVolumeClaim"
    assert data_dir["size"] == "30Gi"
    assert data_dir["storageClass"] == "longhorn"


def test_storage_class_is_omitted_when_unset():
    # Omitted, not empty-string: an empty storageClass means "no
    # provisioner" to Kubernetes, not "use the default".
    values = cluster(storage_class=None).helm_values()
    assert "storageClass" not in values["volume"]["dataDirs"][0]


def test_replication_reaches_both_master_and_filer():
    values = cluster(replication="001").helm_values()
    assert values["master"]["defaultReplication"] == "001"
    assert values["filer"]["defaultReplicaPlacement"] == "001"


def test_s3_gateway_is_always_enabled_and_points_at_the_config_secret():
    c = cluster(name="seaweedfs-test")
    values = c.helm_values()
    assert values["s3"]["enabled"] is True
    assert values["s3"]["port"] == 8333
    assert values["s3"]["existingConfigSecret"] == c.config_secret_name


def test_helm_values_carry_no_credentials():
    # Credentials live in the config Secret this backend applies itself,
    # not in the values passed to Helm.
    c = cluster()
    c.ensure_credentials()
    dumped = str(c.helm_values())
    assert c.access_key not in dumped and c.secret_key not in dumped


def test_extra_values_are_merged():
    assert cluster(extra_values={"foo": "bar"}).helm_values()["foo"] == "bar"


def test_config_secret_name_is_scoped_to_this_cluster():
    c = cluster(name="seaweedfs-test")
    assert c.config_secret_name == "seaweedfs-test-s3-config"


def test_label_selector_targets_this_release():
    assert cluster(name="seaweedfs-test").label_selector == (
        "app.kubernetes.io/instance=seaweedfs-test,app.kubernetes.io/name=seaweedfs"
    )


# -- quantity parsing -----------------------------------------------------
@pytest.mark.parametrize("qty,expected", [
    ("10Gi", 10 * 2**30), ("500M", 500 * 10**6), ("1Ti", 2**40), ("100", 100),
])
def test_parse_quantity(qty, expected):
    assert parse_quantity(qty) == expected


def test_parse_quantity_rejects_unknown_units():
    with pytest.raises(ValueError, match="Unknown unit"):
        parse_quantity("10Zi")
