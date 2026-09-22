"""
Shared machinery for capabilities with interchangeable implementations.

Every capability in this SDK follows one shape: the capability is a class,
the implementation is data.

    Storage(type="longhorn", ...)
    Inference(type="vllm", ...)

A caller asks for the capability and names the implementation once. Code
downstream of the spec — an example, a higher layer, an agent tool — never
learns which implementation is in use, so a capability can gain a second
implementation without anything above the spec changing.

This module holds the parts that would otherwise be copy-pasted into every
capability: type validation, matching the typed options object to the
chosen type, namespace defaulting, and driver dispatch. A new capability
declares its choices and its own fields; it does not reimplement any of
this.

To add a capability
-------------------
1. `multistack/<capability>/spec.py` — the spec model, subclassing
   `CapabilitySpec`, declaring `CAPABILITY`, `SUPPORTED_TYPES`,
   `OPTIONS_FOR_TYPE` and `DEFAULT_NAMESPACES` as `ClassVar`, plus one
   `<Impl>Options` model per implementation.
2. `multistack/<capability>/base.py` — a `Protocol` stating the driver
   contract, plus the capability's catchable error types.
3. `multistack/<capability>/drivers/<impl>.py` — a driver per
   implementation.
4. `multistack/<capability>/registry.py` — the `DRIVERS` map from `type`
   to `(module, class)`, and a backend subclassing `CapabilityBackend`.
5. `multistack/<capability>/__init__.py` — re-exports only, so the
   package's import surface is one short list.

To add an implementation to an existing capability: add the options class,
the driver module, and one entry in each of `OPTIONS_FOR_TYPE` and
`DRIVERS`.
Nothing else in the SDK changes — that is the property this shape exists
to give you.
"""
from __future__ import annotations

import importlib
from typing import Any, ClassVar, Dict, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .kube import require_cluster, require_storage_class


class CapabilitySpec(BaseModel):
    """The contract every capability spec satisfies: choosing an
    implementation via `type`, matching it to a typed `options` object, and
    defaulting the namespace.

    A pydantic model, so a spec cannot exist in an invalid state: the
    checks below run at construction, and `validate_assignment` runs them
    again on any later mutation. `validate()` remains because
    `driver_for()` calls it and because it reads well at a call site, but
    by the time you can call it, it has already passed.

    The declarations below are `ClassVar`, which is what keeps them out of
    the field set. That matters more than it looks: as dataclasses this
    base could not carry fields at all, because a dataclass base forces
    its fields ahead of every subclass's and so dictates field order and
    required-vs-default throughout. pydantic has no such ordering rule, so
    the base can be a real model rather than a mixin.
    """

    model_config = ConfigDict(
        # A misspelled field is a typo, not a value to keep.
        extra="forbid",
        # Re-validates on assignment, so `spec.replica_count = 0` fails
        # where it happens rather than at deploy time.
        validate_assignment=True,
        # Options objects and Node lists are not primitives.
        arbitrary_types_allowed=True,
    )

    # -- each capability declares these -----------------------------------
    # Name used in error messages, e.g. "storage".
    CAPABILITY: ClassVar[str] = "capability"
    # Implementations with a working driver. Only what exists: the SDK
    # does not carry a registry of intended implementations, so `type`
    # accepts exactly what it can actually deploy.
    SUPPORTED_TYPES: ClassVar[Tuple[str, ...]] = ()
    # type -> the options model that type expects.
    OPTIONS_FOR_TYPE: ClassVar[Dict[str, type]] = {}
    # type -> namespace used when the spec leaves it None.
    DEFAULT_NAMESPACES: ClassVar[Dict[str, str]] = {}
    # Capabilities that must already be in place. Machine-readable so a
    # composer can order operations, docs can state it, and an agent can
    # refuse an out-of-order plan. Declaring it does not verify it —
    # each capability verifies its own dependencies in its own terms
    # (see multistack/kube.py).
    REQUIRES: ClassVar[Tuple[str, ...]] = ()

    # check_fields=False because `options` is declared by each capability's
    # own spec, not here.
    @field_validator("options", mode="before", check_fields=False)
    @classmethod
    def _options_match_the_chosen_type(cls, value: Any, info: Any) -> Any:
        """Rejects an options object belonging to another implementation.

        pydantic would catch this on its own, but its message names the
        type mismatch without saying what to do about it. This runs first
        and keeps the instruction.

        A mapping is built into the model `type` names, rather than being
        passed through for pydantic to coerce. That distinction did not
        matter while every capability had one options model — pydantic
        had one candidate and could not choose wrongly. `policy` has two,
        so `options` is a Union, and pydantic resolves a dict against a
        Union by best fit: `Policy(type="tpm", options={"image_tag": ...})`
        produced an `RPMOptions`, because it is the first member and the
        dict fits it. Every tpm-only field then landed as an `extra`
        violation or, worse, silently on the wrong model.

        `type` is the discriminator this capability already has, so use
        it rather than adding one to each options model.
        """
        chosen = (info.data or {}).get("type")
        expected = cls.OPTIONS_FOR_TYPE.get(chosen)
        if isinstance(value, dict):
            return expected(**value) if expected is not None else value
        if value is None:
            return value
        if expected is not None and not isinstance(value, expected):
            raise ValueError(
                f"type='{chosen}' expects options={expected.__name__}, got "
                f"{type(value).__name__}. Leave options unset to get the "
                "right one automatically."
            )
        return value

    @model_validator(mode="after")
    def _fill_in_and_check(self) -> "CapabilitySpec":
        """Fills in the options object matching `type`, then validates.

        Runs at construction and on every assignment, so a caller reads
        `spec.options.<field>` without having built it, and never holds a
        spec that would fail later.
        """
        self.resolve_options()
        self.validate_capability()
        return self

    def resolve_options(self) -> None:
        """Fills in the options object matching `type`."""
        if getattr(self, "options", None) is None:
            options_cls = self.OPTIONS_FOR_TYPE.get(getattr(self, "type", None))
            if options_cls is not None:
                # Bypasses validate_assignment, which would recurse back
                # into this validator.
                object.__setattr__(self, "options", options_cls())

    def validate_capability(self) -> None:
        """The checks every capability spec needs. Call this first from the
        spec's own `validate()`, then add capability-specific rules."""
        chosen = getattr(self, "type", None)

        if not chosen:
            raise ValueError(
                f"type is required — choose one of {self.SUPPORTED_TYPES}. It "
                "is explicit rather than defaulted so the implementation is "
                "always a stated decision."
            )
        if chosen not in self.SUPPORTED_TYPES:
            raise ValueError(
                f"Unknown {self.CAPABILITY} type '{chosen}'. Expected one of "
                f"{self.SUPPORTED_TYPES}."
            )

        namespace = getattr(self, "namespace", None)
        if namespace is not None and not str(namespace).strip():
            raise ValueError(
                "namespace must not be empty — leave it None for the "
                f"{self.CAPABILITY} implementation's default"
            )

        # Typed options exist so an implementation-specific setting handed
        # to the wrong implementation fails here, rather than being
        # silently ignored at deploy time.
        expected = self.OPTIONS_FOR_TYPE.get(chosen)
        if expected is not None:
            actual = getattr(self, "options", None)
            if not isinstance(actual, expected):
                raise ValueError(
                    f"type='{chosen}' expects options={expected.__name__}, got "
                    f"{type(actual).__name__}. Leave options unset to get the "
                    "right one automatically."
                )
            # Implementation-specific rules belong with the
            # implementation's fields, not in a generic spec. An options
            # model validates itself at construction, so this is only for
            # rules it chooses to expose as an explicit method.
            #
            # `"validate" in type(actual).__dict__` rather than getattr:
            # pydantic's BaseModel carries a deprecated `validate`
            # classmethod, so getattr finds one on every options model and
            # calling it raises "BaseModel.validate() missing 1 required
            # positional argument". Following this module's own recipe was
            # enough to hit that.
            own_validate = type(actual).__dict__.get("validate")
            if callable(own_validate):
                own_validate(actual)

    @property
    def resolved_namespace(self) -> str:
        """Where this deploys — the caller's namespace, else the chosen
        implementation's default."""
        chosen = getattr(self, "type", "")
        return getattr(self, "namespace", None) or self.DEFAULT_NAMESPACES.get(
            chosen, chosen
        )


class CapabilityBackend:
    """Dispatches to the driver a spec's `type` selects.

    The single place implementation choice is resolved for a capability, so
    no `if type == ...` appears anywhere else. Subclasses set `DRIVERS` and
    add the capability's methods, each one line:

        def create(self, spec, **kw):
            return self.driver_for(spec).create(spec, **kw)
    """

    # type -> `(module_path, class_name)`, or a driver class directly.
    #
    # The string form is what shipped registries use: the driver's module,
    # and everything it imports, loads only when that type is chosen. So
    # `import multistack` never pays for an implementation nobody asked
    # for, and a driver with a broken optional dependency cannot break
    # importing the SDK. The class form exists for tests and for
    # registering a driver at runtime, where there is no module to name.
    #
    # Keep in step with the spec's SUPPORTED_TYPES and OPTIONS_FOR_TYPE.
    DRIVERS: Dict[str, Union[type, Tuple[str, str]]] = {}

    def __init__(self, **driver_kwargs: Any):
        # Passed to whichever driver gets selected, so driver-specific
        # timeouts still work. Not validated here: different drivers accept
        # different ones, and no type is chosen yet.
        self._driver_kwargs = driver_kwargs
        self._cache: Dict[str, Any] = {}

    def driver_for(self, spec: Any) -> Any:
        """The driver `spec.type` selects.

        Validates the spec, then verifies its declared `REQUIRES` are
        actually satisfied, so an unknown type or a missing dependency
        fails with a specific message instead of a `KeyError` — or worse,
        an opaque failure from inside helm several minutes later. Drivers
        are cached per type, so repeated calls reuse one instance.
        """
        spec.validate()
        self.verify_requirements(spec)
        chosen = spec.type
        if chosen not in self._cache:
            self._cache[chosen] = self.driver_class(chosen)(**self._driver_kwargs)
        return self._cache[chosen]

    @classmethod
    def driver_class(cls, chosen: str) -> type:
        """The driver class registered for `chosen`, imported if needed.

        Split out from `driver_for` so the registry can be checked without
        constructing anything — which is how a test verifies that every
        entry resolves, since lazy loading otherwise defers a typo in
        `DRIVERS` to whenever someone first selects that type.
        """
        entry = cls.DRIVERS.get(chosen)
        if entry is None:
            # Reachable only if DRIVERS and SUPPORTED_TYPES disagree,
            # which is a packaging bug rather than a user error.
            raise ValueError(
                f"No driver registered for type='{chosen}', although the "
                "spec accepts it. DRIVERS and SUPPORTED_TYPES are out of "
                "step."
            )
        if isinstance(entry, type):
            return entry

        module_path, class_name = entry
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            # The cost of loading lazily: a dependency problem surfaces
            # here rather than at `import multistack`. Say which
            # implementation, so the answer isn't "something failed to
            # import" — most often this is a driver's optional dependency
            # that was never installed.
            raise ImportError(
                f"the '{chosen}' driver could not be imported from "
                f"{module_path}: {exc}. Its dependencies may not be "
                "installed."
            ) from exc
        try:
            return getattr(module, class_name)
        except AttributeError as exc:
            raise ValueError(
                f"{module_path} has no {class_name}, which DRIVERS names as "
                f"the driver for type='{chosen}'."
            ) from exc

    @classmethod
    def resolve_all(cls) -> Dict[str, type]:
        """Every registered driver class, imported. Only for tests and
        diagnostics — it defeats lazy loading on purpose, so that a broken
        registry entry fails in CI instead of in front of a user."""
        return {chosen: cls.driver_class(chosen) for chosen in cls.DRIVERS}

    def verify_requirements(self, spec: Any) -> None:
        """Checks the capabilities `spec.REQUIRES` names are in place.

        Only `cluster` is verifiable generically — a reachable API server
        means the same thing to everyone. Other dependencies are
        capability-specific (an object store's dependency on block storage
        is "a StorageClass exists"), so a capability overrides this, calls
        `super()`, and adds its own.
        """
        requires = getattr(spec, "REQUIRES", ())
        capability = getattr(spec, "CAPABILITY", "this capability")
        kubeconfig_path = getattr(spec, "kubeconfig_path", "")

        if "cluster" in requires:
            require_cluster(kubeconfig_path, capability=capability)

        if "storage" in requires:
            # A capability that claims volumes needs a class that exists
            # now. A backend with a better check of its own overrides this
            # and does not call super() for the storage part — MinIO's
            # version separates a named-but-absent class (fatal) from no
            # default class (a warning), which this cannot know.
            require_storage_class(
                kubeconfig_path,
                getattr(spec, "storage_class", None),
                capability=capability,
            )

    @classmethod
    def requirements(cls, spec_cls: type) -> Tuple[str, ...]:
        """What this capability needs in place first. Machine-readable, so
        a composer can order operations and an agent can refuse an
        out-of-order plan."""
        return tuple(getattr(spec_cls, "REQUIRES", ()))

    @classmethod
    def implementations(cls) -> Tuple[str, ...]:
        """Which implementations this backend can actually dispatch to."""
        return tuple(sorted(cls.DRIVERS))
