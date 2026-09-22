"""Tests for the Storage spec: implementation selection via `type`,
the generic/implementation-specific field split, and validation."""
import pytest

from multistack import Storage
from multistack.storage import LonghornOptions

KUBECONFIG = "/tmp/kc.yaml"


class OtherOptions:
    """Stands in for another implementation's options. Deliberately local:
    the wrong-options check must hold without a second implementation
    existing in the SDK to borrow one from."""


def storage(**kwargs) -> Storage:
    defaults = dict(type="longhorn", kubeconfig_path=KUBECONFIG)
    return Storage(**{**defaults, **kwargs})


# -- implementation selection ---------------------------------------------
def test_type_is_required_and_not_defaulted():
    # Defaulting it would hide the choice this class exists to expose.
    with pytest.raises(ValueError, match="type is required"):
        Storage(type="", kubeconfig_path=KUBECONFIG).validate()


def test_unknown_type_is_rejected():
    with pytest.raises(ValueError, match="Unknown storage type"):
        storage(type="gluster").validate()


# -- options: typed, and matched to the type ------------------------------
def test_options_default_to_the_type_they_belong_to():
    # So callers reach spec.options.kubelet_root_dir without building it.
    assert isinstance(storage().options, LonghornOptions)


def test_options_from_the_wrong_implementation_are_rejected():
    # The whole point of typing options instead of using a dict: options
    # belonging to a different implementation fail here, rather than being
    # silently ignored at deploy time.
    with pytest.raises(ValueError, match="expects options=LonghornOptions"):
        storage(options=OtherOptions()).validate()


def test_longhorn_defaults_pin_the_kubelet_root_dir():
    # Longhorn's own auto-detection reads kubelet's cmdline via pod logs
    # and fails when the API server can't reach a node — taking the whole
    # CSI driver with it.
    assert storage().options.kubelet_root_dir == "/var/lib/kubelet"


@pytest.mark.parametrize("field", ["data_path", "kubelet_root_dir"])
def test_relative_paths_are_rejected(field):
    with pytest.raises(ValueError, match="absolute path"):
        storage(options=LonghornOptions(**{field: "relative/path"})).validate()


@pytest.mark.parametrize("field", ["release_name"])
def test_empty_option_names_are_rejected(field):
    with pytest.raises(ValueError, match="must not be empty"):
        storage(options=LonghornOptions(**{field: ""})).validate()


# -- generic fields -------------------------------------------------------
def test_valid_spec_passes():
    storage().validate()


def test_kubeconfig_path_is_required():
    # Never ambient: a stale default kubeconfig targets the wrong cluster.
    with pytest.raises(ValueError, match="kubeconfig_path is required"):
        storage(kubeconfig_path="").validate()


@pytest.mark.parametrize("count", [0, -1])
def test_non_positive_replica_count_is_rejected(count):
    with pytest.raises(ValueError, match="at least 1"):
        storage(replica_count=count).validate()


def test_namespace_defaults_per_implementation():
    assert storage().resolved_namespace == "longhorn-system"


def test_explicit_namespace_wins():
    assert storage(namespace="storage").resolved_namespace == "storage"


def test_blank_namespace_is_rejected_rather_than_treated_as_default():
    # `helm --namespace ""` does something, and it isn't what you meant.
    with pytest.raises(ValueError, match="must not be empty"):
        storage(namespace="   ").validate()


def test_storage_class_name_is_the_type():
    # This is what a consuming spec (a MinIO tenant, a PVC) should name.
    assert storage().storage_class_name == "longhorn"
