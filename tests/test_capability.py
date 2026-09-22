"""Tests for the shared capability machinery: type dispatch, driver
caching, dependency verification, and registry consistency.

These cover `multistack/capability.py` itself rather than any one
capability, so a new capability inherits working behaviour instead of
re-testing it."""
from typing import Any, ClassVar, Dict, Optional

import pytest
from pydantic import BaseModel, ConfigDict

from multistack import Storage, StorageBackend
from multistack.capability import CapabilityBackend, CapabilitySpec
from multistack.kube import ClusterDependencyError
from multistack.storage import DRIVERS as STORAGE_DRIVERS
from multistack.storage import StorageBackend as RealStorageBackend


# -- a throwaway capability, so these tests don't depend on a real one ----
class AlphaOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    setting: str = "a"

    def validate(self) -> None:
        if not self.setting:
            raise ValueError("setting must not be empty")


class Fake(CapabilitySpec):
    type: str
    kubeconfig_path: str = "/tmp/kc.yaml"
    namespace: Optional[str] = None
    options: Optional[AlphaOptions] = None

    CAPABILITY: ClassVar[str] = "fake"
    SUPPORTED_TYPES: ClassVar[tuple] = ("alpha",)
    OPTIONS_FOR_TYPE: ClassVar[dict] = {"alpha": AlphaOptions}
    DEFAULT_NAMESPACES: ClassVar[dict] = {"alpha": "alpha-system"}

    def validate(self) -> None:
        self.validate_capability()


class AlphaDriver:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeBackend(CapabilityBackend):
    DRIVERS = {"alpha": AlphaDriver}


@pytest.fixture
def backend():
    return FakeBackend()


# -- dispatch -------------------------------------------------------------
def test_driver_is_selected_from_type(backend):
    assert isinstance(backend.driver_for(Fake(type="alpha")), AlphaDriver)


def test_unknown_type_fails_with_the_spec_message_not_a_keyerror(backend):
    with pytest.raises(ValueError, match="Unknown fake type"):
        backend.driver_for(Fake(type="gamma"))


def test_drivers_are_cached_per_type(backend):
    # Repeated calls must not rebuild the driver — some hold connections.
    assert backend.driver_for(Fake(type="alpha")) is backend.driver_for(Fake(type="alpha"))


def test_constructor_kwargs_reach_the_driver():
    assert FakeBackend(ready_timeout=99).driver_for(Fake(type="alpha")).kwargs == {
        "ready_timeout": 99
    }


def test_error_messages_name_the_capability(backend):
    # The mixin knows CAPABILITY, so every capability gets this free.
    with pytest.raises(ValueError, match="fake"):
        backend.driver_for(Fake(type="gamma"))


# -- options --------------------------------------------------------------
def test_options_are_filled_in_for_the_chosen_type():
    assert isinstance(Fake(type="alpha").options, AlphaOptions)


def test_options_validate_is_called_through_the_spec():
    # Implementation rules live with implementation fields, but a caller
    # only ever calls spec.validate().
    with pytest.raises(ValueError, match="setting must not be empty"):
        Fake(type="alpha", options=AlphaOptions(setting="")).validate()


def test_namespace_falls_back_to_the_implementation_default():
    assert Fake(type="alpha").resolved_namespace == "alpha-system"
    assert Fake(type="alpha", namespace="mine").resolved_namespace == "mine"


# -- dependency verification ----------------------------------------------
def test_no_cluster_check_when_nothing_is_required(backend, monkeypatch):
    # Fake declares no REQUIRES, so a bogus kubeconfig is irrelevant to it.
    def explode(*a, **k):
        pytest.fail("require_cluster should not be called")

    monkeypatch.setattr("multistack.capability.require_cluster", explode)
    backend.driver_for(Fake(type="alpha", kubeconfig_path="/nope.yaml"))


def test_declared_cluster_requirement_is_verified(monkeypatch):
    calls = []

    def fake_require(path, capability="?", **k):
        calls.append((path, capability))

    monkeypatch.setattr("multistack.capability.require_cluster", fake_require)
    StorageBackend().driver_for(
        Storage(type="longhorn", kubeconfig_path="/tmp/kc.yaml", replica_count=1)
    )
    assert calls == [("/tmp/kc.yaml", "storage")]


def test_unmet_cluster_dependency_blocks_before_any_work(monkeypatch):
    def refuse(path, capability="?", **k):
        raise ClusterDependencyError(f"{capability} needs a cluster")

    monkeypatch.setattr("multistack.capability.require_cluster", refuse)
    with pytest.raises(ClusterDependencyError, match="storage needs a cluster"):
        StorageBackend().driver_for(
            Storage(type="longhorn", kubeconfig_path="/tmp/kc.yaml", replica_count=1)
        )


def test_storage_declares_its_cluster_dependency():
    # Machine-readable, so a composer can order operations.
    assert StorageBackend.requirements(Storage) == ("cluster",)


# -- registry consistency -------------------------------------------------
def test_every_supported_type_has_a_driver():
    # SUPPORTED_TYPES and DRIVERS are declared in different files and drift
    # silently; a type accepted by the spec with no driver behind it is a
    # packaging bug that only shows up at runtime.
    assert set(Storage.SUPPORTED_TYPES) == set(STORAGE_DRIVERS)


def test_every_driver_has_an_options_class():
    for name in STORAGE_DRIVERS:
        assert name in Storage.OPTIONS_FOR_TYPE, f"{name} has no options class"


def test_implementations_lists_what_can_actually_be_dispatched():
    assert StorageBackend.implementations() == ("longhorn",)


# -- lazy driver loading ---------------------------------------------------
def test_registry_names_drivers_rather_than_importing_them():
    # The property this buys: `import multistack` costs nothing per
    # implementation. A class here instead of a (module, class) pair would
    # mean the registry's module imported the driver to build it.
    for chosen, entry in STORAGE_DRIVERS.items():
        assert isinstance(entry, tuple), f"{chosen} is eagerly imported"
        assert len(entry) == 2


def test_importing_the_sdk_loads_no_driver():
    # Guards the whole point of the string registry. Run in a clean
    # interpreter, because this session has almost certainly imported a
    # driver by now.
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, multistack\n"
         "from multistack.storage import StorageBackend\n"
         "loaded = [m for m in sys.modules if '.drivers.' in m]\n"
         "print(loaded)"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout


def test_every_registry_entry_actually_resolves():
    # Lazy loading defers a typo in DRIVERS to whenever someone first
    # selects that type. This is what stops that being a user's problem.
    resolved = RealStorageBackend.resolve_all()
    assert set(resolved) == set(STORAGE_DRIVERS)
    for chosen, driver_cls in resolved.items():
        assert isinstance(driver_cls, type), chosen


def test_a_bad_module_path_names_the_implementation():
    class Broken(CapabilityBackend):
        DRIVERS = {"alpha": ("multistack.storage.drivers.nonexistent", "X")}

    with pytest.raises(ImportError, match="'alpha' driver could not be imported"):
        Broken.driver_class("alpha")


def test_a_bad_class_name_says_which_module_lacks_it():
    class Broken(CapabilityBackend):
        DRIVERS = {"alpha": ("multistack.storage.drivers.longhorn", "NoSuchDriver")}

    with pytest.raises(ValueError, match="has no NoSuchDriver"):
        Broken.driver_class("alpha")


def test_a_driver_class_can_still_be_registered_directly():
    # For tests, and for registering a driver at runtime, where there is
    # no importable module to name.
    assert FakeBackend.driver_class("alpha") is AlphaDriver


# -- what the pydantic models buy ----------------------------------------
def test_a_spec_validates_at_construction_not_later():
    # The point of the model: a spec that exists is one that passed. Under
    # dataclasses an invalid spec could be built and only failed when
    # someone remembered to call validate().
    with pytest.raises(ValueError, match="Unknown storage type"):
        Storage(type="gluster", kubeconfig_path="/tmp/kc.yaml")


def test_mutating_a_spec_into_an_invalid_state_fails_at_the_assignment():
    storage = Storage(type="longhorn", kubeconfig_path="/tmp/kc.yaml")
    with pytest.raises(ValueError, match="at least 1"):
        storage.replica_count = 0
    # And the spec is unchanged, rather than half-updated.
    assert storage.replica_count == 3


def test_a_misspelled_field_is_refused():
    # extra="forbid". A silently-ignored `replica_cont=1` is how a spec
    # ends up not doing what the file says it does.
    with pytest.raises(ValueError, match="replica_cont"):
        Storage(type="longhorn", kubeconfig_path="/tmp/kc.yaml", replica_cont=1)


def test_a_spec_can_be_built_from_a_plain_dict():
    # What makes loading a spec from YAML or JSON possible: the nested
    # options mapping is coerced into the right options model.
    storage = Storage.model_validate({
        "type": "longhorn",
        "kubeconfig_path": "/tmp/kc.yaml",
        "replica_count": 2,
        "options": {"data_path": "/mnt/longhorn"},
    })
    assert storage.replica_count == 2
    assert storage.options.data_path == "/mnt/longhorn"
    assert storage.options.kubelet_root_dir == "/var/lib/kubelet"   # default kept


def test_a_spec_round_trips_through_json():
    storage = Storage(type="longhorn", kubeconfig_path="/tmp/kc.yaml")
    assert Storage.model_validate_json(storage.model_dump_json()) == storage


def test_an_invalid_dict_is_refused_on_the_way_in():
    # The reason round-tripping is safe: a hand-edited file cannot smuggle
    # in a spec that would fail at deploy time.
    with pytest.raises(ValueError, match="must be an absolute path"):
        Storage.model_validate({
            "type": "longhorn",
            "kubeconfig_path": "/tmp/kc.yaml",
            "options": {"data_path": "relative/path"},
        })


def test_the_capability_contract_is_not_part_of_the_field_set():
    # CAPABILITY, SUPPORTED_TYPES and friends are ClassVar. If any leaked
    # into the fields, callers could pass them as constructor arguments
    # and a spec would carry its own contract as data.
    fields = set(Storage.model_fields)
    for name in ("CAPABILITY", "SUPPORTED_TYPES", "OPTIONS_FOR_TYPE",
                 "DEFAULT_NAMESPACES", "REQUIRES"):
        assert name not in fields, name


def test_a_declared_storage_dependency_is_verified(monkeypatch):
    """`verify_requirements` handles the generic half of every declared
    dependency. A capability with a better check of its own overrides it —
    MinIO does — but a capability that declares "storage" and does nothing
    else must still be checked."""
    seen = {}

    class NeedsStorage(Fake):
        REQUIRES = ("cluster", "storage")

    monkeypatch.setattr("multistack.capability.require_cluster",
                        lambda path, capability="?", **k: "ok")
    monkeypatch.setattr(
        "multistack.capability.require_storage_class",
        lambda path, name=None, capability="?", **k: seen.update(
            path=path, name=name, capability=capability) or "longhorn",
    )
    FakeBackend().driver_for(NeedsStorage(type="alpha"))
    assert seen == {"path": "/tmp/kc.yaml", "name": None, "capability": "fake"}


def test_an_unmet_storage_dependency_blocks_before_any_work(monkeypatch):
    from multistack.kube import ClusterDependencyError

    class NeedsStorage(Fake):
        REQUIRES = ("storage",)

    monkeypatch.setattr(
        "multistack.capability.require_storage_class",
        lambda *a, **k: (_ for _ in ()).throw(
            ClusterDependencyError("no StorageClass at all")),
    )
    with pytest.raises(ClusterDependencyError, match="no StorageClass"):
        FakeBackend().driver_for(NeedsStorage(type="alpha"))


def test_a_dict_for_options_is_built_from_the_chosen_type():
    """With two options models, `options` is a Union — and pydantic
    resolves a dict against a Union by best fit, not by `type`.

    `Policy(type="tpm", options={"image_tag": ...})` produced an
    RPMOptions, because it is the first member of the Union and the dict
    fits it. Every tpm-only field then failed as an `extra` violation, or
    landed on the wrong model. `type` is the discriminator the capability
    already has, so the validator builds the model it names.
    """
    from multistack import Policy
    from multistack.policy.spec import RPMOptions, TPMOptions

    shared = dict(kubeconfig_path="/tmp/kc.yaml",
                  cache_url="redis://valkey:6379/0",
                  event_backbone_url="nats://nats:4222")

    rpm = Policy(type="rpm", options={"image_tag": "0.1.0"}, **shared)
    assert isinstance(rpm.options, RPMOptions)
    assert rpm.options.durable_name == "rpm-counter"

    tpm = Policy(type="tpm", options={"image_tag": "0.1.0"}, **shared)
    assert isinstance(tpm.options, TPMOptions)
    assert tpm.options.durable_name == "tpm-counter-enriched"

    # A tpm-only field is the case that failed outright before.
    assert Policy(
        type="tpm", options={"event_stream_subject": "gateway.events.enriched.v2"},
        **shared,
    ).options.event_stream_subject == "gateway.events.enriched.v2"


# -- every capability's registry, not just storage's ----------------------
#
# The registry names drivers as (module, class) strings so importing the
# SDK costs nothing per driver. The price is that a wrong module path, a
# renamed class, or a driver importing a name that does not exist is
# invisible until someone chooses that `type` -- at which point it fails
# at deploy time, not at test time.
#
# The observability capability shipped exactly that: its driver did
# `from ...helm import PROMETHEUS_COMMUNITY_REPOSITORY`, a name defined
# nowhere in the package. The module could not be imported at all, so the
# capability was entirely non-functional, and nothing failed -- the tests
# above only ever resolved the storage registry.

def _every_registry():
    """(capability_name, DRIVERS) for every capability that has one."""
    import importlib
    import pkgutil

    import multistack

    found = []
    for module in pkgutil.iter_modules(multistack.__path__):
        if not module.ispkg:
            continue
        try:
            registry = importlib.import_module(
                f"multistack.{module.name}.registry"
            )
        except ModuleNotFoundError:
            continue        # not a capability package
        drivers = getattr(registry, "DRIVERS", None)
        if drivers:
            found.append((module.name, drivers))
    return found


def test_the_sweep_finds_every_capability_package():
    # Guards the guard: a discovery bug here would make every assertion
    # below pass by finding nothing.
    names = {name for name, _ in _every_registry()}
    assert {
        "storage", "gateway", "inference", "ingress_gateway",
        "policy", "observability", "tokenizer", "controlplane", "portal",
    } <= names, f"capability packages missed by discovery: {names}"


@pytest.mark.parametrize(
    "capability,drivers",
    _every_registry(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_every_registered_driver_actually_imports(capability, drivers):
    import importlib

    for chosen, entry in drivers.items():
        module_path, class_name = entry
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            pytest.fail(
                f"{capability} registers '{chosen}' -> {module_path}, which "
                f"does not import: {exc}"
            )
        assert hasattr(module, class_name), (
            f"{capability} registers '{chosen}' -> {module_path}.{class_name}, "
            f"but that module has no {class_name}"
        )
