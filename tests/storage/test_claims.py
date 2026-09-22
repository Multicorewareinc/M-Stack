"""Tests for the VolumeClaim spec and StorageBackend's claim lifecycle.

Incorporated from a teammate's standalone longhorn_k8s_sdk, which had the
PVC surface ours lacked. Reimplemented on the capability rather than in a
driver: a claim names a StorageClass, and every block-storage
implementation produces one, so there is nothing here for an
implementation to do differently.
"""
from __future__ import annotations

import pytest

from multistack import Storage, VolumeClaim
from multistack.storage import ACCESS_MODES, StorageBackend, StorageError
from multistack.storage.registry import _as_bytes

KUBECONFIG = "/tmp/kc.yaml"


@pytest.fixture
def storage():
    return Storage(type="longhorn", kubeconfig_path=KUBECONFIG)


@pytest.fixture
def backend(monkeypatch):
    """A backend whose kubectl/apply calls are recorded, not run."""
    calls = []
    b = StorageBackend()
    b.recorded = calls

    def fake_kubectl(kubeconfig, *args, **kwargs):
        calls.append(("kubectl",) + args)
        joined = " ".join(args)
        if "allowVolumeExpansion" in joined:
            return "true"
        if ".spec.resources.requests.storage" in joined:
            return "10Gi"
        if "get" in args and "-o" in args and "name" in args:
            return ""                       # gone, for wait_for_absent
        if "jsonpath={.status.phase}" in joined:
            return "Bound"
        return ""

    def fake_apply(kubeconfig, manifest, **kwargs):
        calls.append(("apply", manifest))
        return "created"

    monkeypatch.setattr("multistack.kube.kubectl", fake_kubectl)
    monkeypatch.setattr("multistack.kube.apply", fake_apply)
    return b


# -- the spec -------------------------------------------------------------
def test_size_needs_a_unit():
    # Kubernetes reads a bare "10" as ten bytes: a claim that binds and is
    # immediately full.
    with pytest.raises(ValueError, match="bare number is bytes"):
        VolumeClaim(name="data", size="10")


@pytest.mark.parametrize("size", ["10Gi", "500Mi", "1Ti", "1.5Gi"])
def test_valid_quantities_are_accepted(size):
    assert VolumeClaim(name="data", size=size).size == size


def test_unknown_access_mode_is_rejected_and_names_the_valid_ones():
    with pytest.raises(ValueError, match="Unknown access_mode"):
        VolumeClaim(name="data", access_mode="RWX")


@pytest.mark.parametrize("mode", ACCESS_MODES)
def test_every_declared_access_mode_is_accepted(mode):
    assert VolumeClaim(name="data", access_mode=mode).access_mode == mode


def test_manifest_shape():
    claim = VolumeClaim(name="data", namespace="app", size="10Gi")
    manifest = claim.manifest("longhorn")
    assert manifest["kind"] == "PersistentVolumeClaim"
    assert manifest["metadata"] == {"name": "data", "namespace": "app"}
    assert manifest["spec"]["storageClassName"] == "longhorn"
    assert manifest["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert manifest["spec"]["resources"]["requests"]["storage"] == "10Gi"


# -- which StorageClass ---------------------------------------------------
def test_claim_defaults_to_the_class_the_storage_spec_produces(backend, storage):
    # The thing callers get wrong by hardcoding a name.
    used = backend.create_claim(storage, VolumeClaim(name="data"), wait=False)
    assert used == storage.storage_class_name == "longhorn"


def test_an_explicit_class_on_the_claim_wins(backend, storage):
    used = backend.create_claim(
        storage, VolumeClaim(name="data", storage_class="other"), wait=False
    )
    assert used == "other"


# -- create ---------------------------------------------------------------
def test_create_applies_rather_than_creates(backend, storage):
    """apply, so re-running a provisioning script is a no-op instead of a
    409 conflict."""
    backend.create_claim(storage, VolumeClaim(name="data"), wait=False)
    assert [c[0] for c in backend.recorded] == ["apply"]


def test_create_waits_for_bound_by_default(backend, storage):
    # An unbound PVC is not an error — it stays Pending, and whatever
    # mounts it fails instead.
    backend.create_claim(storage, VolumeClaim(name="data"))
    assert any("jsonpath={.status.phase}" in " ".join(c[1:])
               for c in backend.recorded if c[0] == "kubectl")


# -- resize ---------------------------------------------------------------
def test_resize_refuses_to_shrink(backend, storage):
    # The API server rejects a shrink with a message about immutability
    # that never mentions shrinking.
    with pytest.raises(StorageError, match="cannot shrink"):
        backend.resize_claim(storage, VolumeClaim(name="data", size="10Gi"), "5Gi")


def test_resize_grows(backend, storage):
    claim = VolumeClaim(name="data", size="10Gi")
    backend.resize_claim(storage, claim, "20Gi")
    assert claim.size == "20Gi"
    assert any("patch" in c for c in backend.recorded)


def test_resize_refuses_a_class_without_expansion(backend, storage, monkeypatch):
    """A class without allowVolumeExpansion accepts the patch and then
    silently never resizes, which is worse than failing."""
    monkeypatch.setattr(
        "multistack.kube.kubectl",
        lambda kubeconfig, *a, **k: "false" if "allowVolumeExpansion" in " ".join(a) else "10Gi",
    )
    with pytest.raises(StorageError, match="does not allow volume expansion"):
        backend.resize_claim(storage, VolumeClaim(name="data"), "20Gi")


# -- delete ---------------------------------------------------------------
def test_delete_is_idempotent(backend, storage):
    backend.delete_claim(storage, VolumeClaim(name="data"), wait=False)
    assert any("--ignore-not-found" in c for c in backend.recorded)


def test_delete_waits_for_the_name_to_be_free(backend, storage):
    # A PVC with a volume attached stays Terminating until the CSI driver
    # releases it, so recreating the same name too early fails.
    backend.delete_claim(storage, VolumeClaim(name="data"))
    assert any("get" in c and "pvc" in c for c in backend.recorded)


# -- quantity comparison --------------------------------------------------
@pytest.mark.parametrize("bigger,smaller", [
    ("10Gi", "9Gi"), ("1Ti", "999Gi"), ("1Gi", "500Mi"), ("1G", "900M"),
])
def test_quantities_compare_by_value_not_string(bigger, smaller):
    # String comparison would call '9Gi' larger than '10Gi'.
    assert _as_bytes(bigger) > _as_bytes(smaller)
