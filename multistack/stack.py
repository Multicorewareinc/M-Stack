"""
Composing capabilities: say the shared facts once, wire the rest.

    stack = Stack(kubeconfig_path="~/.multistack/kubeconfig")

    storage = stack.build(Storage, type="longhorn")
    StorageBackend().create(storage, nodes=nodes)
    stack.record(storage)                       # publishes its StorageClass

    tenant = stack.build(MinIOTenant, name="minio", servers=2)
    #  -> kubeconfig_path and storage_class both filled in

Every layer needs the layer below it. The kubeconfig threads through all
of them; the StorageClass that block storage produces is what an object
store's volume claims bind against; the object store's endpoint is where
inference reads its weights. Written out by hand that is the same few
values repeated in every spec, and `examples/full_stack.py` repeated
`kubeconfig_path=KUBECONFIG` five times and hardcoded
`storage_class="longhorn"` twice — each one a place to get it wrong, and
the hardcoded class silently wrong the moment the implementation changes.

Not ambient, and that distinction is the whole point
------------------------------------------------------
This SDK refuses to read `$KUBECONFIG` or `~/.kube/config`, because a
stale default silently targets the wrong cluster. A `Stack` is the
opposite of that: one explicit statement of which cluster, at the top of
the file, threaded down. Nothing is discovered from the environment, and
`stack.outputs` shows exactly what will be filled in.

Specs stay standalone. `Storage(type="longhorn", kubeconfig_path=...)`
works with no Stack anywhere, and every example that does so still does.
A Stack is a convenience for composition, never a requirement.

How a spec takes part
---------------------
Two class-level declarations, both optional:

    FROM_STACK = {"kubeconfig_path": "kubeconfig_path"}   # field <- stack key
    PROVIDES   = {"storage_class": "storage_class_name"}  # stack key <- attribute

`build()` fills a declared field only when the caller did not pass it, so
an explicit argument always wins. `record()` reads a spec's `PROVIDES`
and publishes them for whatever comes next.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


# What each capability publishes once it has been created and recorded.
# This is what lets a spec's REQUIRES be checked at wiring time rather
# than at deploy time — the difference between "you built these in the
# wrong order" and a readiness timeout ten minutes later.
CAPABILITY_OUTPUT = {
    "cluster": "kubeconfig_path",
    "storage": "storage_class",
    "objectstore": "s3_endpoint_url",
    # "cache", not "valkey" — the key names the capability a dependent
    # asks for, not the implementation providing it, which is the same
    # reason the state layer records a Valkey as "cache".
    "cache": "cache_url",
    "database": "database_url",
    "inference": "inference_endpoint",
    "policy": "policy_endpoint",
    "gateway": "gateway_endpoint",
    "ingress_gateway": "ingress_gateway_endpoint",
    "route": "route_url",
    "observability": "observability_endpoint",
    "tokenizer": "tokenizer_url",
    # Two control planes and two portals are two *instances* of one
    # capability, the same way rpm and tpm are two policies. A Stack
    # holding both publishes the last recorded, which is why a portal
    # that must point at a specific one sets api_upstream directly.
    "controlplane": "controlplane_endpoint",
    "portal": "portal_endpoint",
    "billing": "billing_endpoint",
    "queue": "event_backbone_url",
}


class MissingDependencyError(RuntimeError):
    """Raised when a spec needs a value no earlier layer has published."""


class Stack:
    """The shared facts of one deployment, and what each layer produced.

    Holds values, not objects: a `Stack` never creates anything and never
    talks to a cluster. Backends still do the work, and are still called
    explicitly, so what happens stays visible in the file.
    """

    def __init__(self, kubeconfig_path: str, **values: Any) -> None:
        if not kubeconfig_path:
            raise ValueError(
                "kubeconfig_path is required — a Stack exists to state which "
                "cluster once, explicitly. There is no ambient fallback."
            )
        self._values: Dict[str, Any] = {"kubeconfig_path": kubeconfig_path}
        self._values.update({k: v for k, v in values.items() if v is not None})

    # -- reading -----------------------------------------------------------
    @property
    def outputs(self) -> Mapping[str, Any]:
        """Everything published so far, as a read-only view.

        Worth printing when a wiring error is confusing: it is the exact
        set of values `build()` can fill in.
        """
        return dict(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._values

    # -- writing -----------------------------------------------------------
    def provide(self, **values: Any) -> "Stack":
        """Publishes values for later layers. Returns self, so it chains.

        For anything a spec cannot derive on its own — a generated
        credential, an endpoint that only exists once something is
        running, the Secret someone chose to write it to.
        """
        for key, value in values.items():
            if value is None:
                raise ValueError(
                    f"refusing to publish {key}=None. A missing value should "
                    "stay missing, so the error names the layer that owes it "
                    "rather than surfacing as a null three specs later."
                )
            self._values[key] = value
        return self

    def record(self, spec: Any) -> "Stack":
        """Publishes what `spec` declares in `PROVIDES`.

        Call it *after* the backend has created the thing. A StorageClass
        name is knowable from the spec before anything exists, and
        publishing it early would let a later layer bind against a class
        that has not been installed yet — which is precisely the failure
        `require_storage_class` exists to catch.
        """
        for key, attribute in getattr(spec, "PROVIDES", {}).items():
            value = getattr(spec, attribute, None)
            if callable(value):
                value = value()
            if value is not None:
                self._values[key] = value
        return self

    # -- building ----------------------------------------------------------
    def build(self, spec_cls: type, **kwargs: Any) -> Any:
        """Constructs `spec_cls`, filling declared fields from the stack.

        Fills, then constructs — so the spec's own validation sees a
        complete set of values and a spec still cannot exist in a state it
        would reject. An argument passed here always beats the stack.
        """
        self._check_order(spec_cls)

        from_stack: Mapping[str, str] = getattr(spec_cls, "FROM_STACK", {})
        resolved = dict(kwargs)
        unmet = []

        for field, key in from_stack.items():
            if field in resolved:
                continue                      # explicit beats wired
            if key in self._values:
                resolved[field] = self._values[key]
            elif self._is_required(spec_cls, field):
                unmet.append((field, key))

        if unmet:
            raise MissingDependencyError(self._unmet_message(spec_cls, unmet))

        return spec_cls(**resolved)

    def _check_order(self, spec_cls: type) -> None:
        """Refuses to build a spec whose dependencies have not been recorded.

        A spec declares `REQUIRES`, and each capability publishes one value
        when recorded. If a required capability has published nothing, the
        layers are being built in the wrong order — and catching that here
        costs a line, where letting it through costs a chart install and a
        readiness timeout that blames the wrong component.

        Only checked inside a Stack. Constructing the spec directly still
        works, for the case where a dependency was satisfied outside the
        SDK; the backend's own `require_*` checks still verify the cluster
        really has what the spec claims.
        """
        missing = [
            (capability, CAPABILITY_OUTPUT[capability])
            for capability in getattr(spec_cls, "REQUIRES", ())
            if capability in CAPABILITY_OUTPUT
            and CAPABILITY_OUTPUT[capability] not in self._values
        ]
        if not missing:
            return
        lines = [
            f"{spec_cls.__name__} requires the '{capability}' capability, but "
            f"nothing has published '{key}' to this Stack."
            for capability, key in missing
        ]
        raise MissingDependencyError(
            "\n".join(lines)
            + "\n\nCreate that layer first, then stack.record(its_spec). "
            + f"Published so far: {sorted(self._values)}."
            + "\n(Or build the spec directly if the dependency was satisfied "
              "outside the SDK — the backend still verifies it for real.)"
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _is_required(spec_cls: type, field: str) -> bool:
        """Whether `field` has no default, so leaving it out would fail."""
        model_fields = getattr(spec_cls, "model_fields", {})
        info = model_fields.get(field)
        return bool(info) and info.is_required()

    def _unmet_message(self, spec_cls: type, unmet: list) -> str:
        """Names the layer that owes each missing value.

        The point of this class: "field required" tells you a field is
        missing, not which capability was supposed to have produced it.
        """
        owed_by = {
            "kubeconfig_path": "the cluster capability (see examples/rke2/)",
            "storage_class": "the storage capability — create Storage, then "
                             "stack.record(storage)",
            "s3_endpoint_url": "the object store — create a tenant, then "
                               "stack.record(tenant)",
            "s3_secret_name": "whoever wrote the credentials Secret; publish "
                              "it with stack.provide(s3_secret_name=...)",
            "inference_endpoint": "the inference capability — create "
                                  "Inference, then stack.record(inference)",
            "policy_endpoint": "the policy capability — create Policy, then "
                               "stack.record(policy)",
            "database_url": "the database capability — create a CNPG "
                            "cluster with create_cluster(), then "
                            "stack.record(cnpg)",
            "database_secret_name": "CloudNativePG, which writes it as "
                                    "<cluster>-app-secret; publish it with "
                                    "stack.provide(database_secret_name=...)",
            "cache_url": "the cache capability — create Valkey, then "
                         "stack.record(valkey). Or publish it with "
                         "stack.provide(cache_url=...) for a cache that "
                         "was not deployed through the SDK",
            "event_backbone_url": "the event backbone; publish it with "
                                  "stack.provide(event_backbone_url=...)",
            "api_key_secret": "whoever created the gateway's API key Secret; "
                              "publish it with "
                              "stack.provide(api_key_secret=...)",
            "tokenizer_url": "the tokenizer capability — create Tokenizer, "
                             "then stack.record(tokenizer)",
            "controlplane_endpoint": "a control plane — create ControlPlane, "
                                     "then stack.record(it)",
            "portal_endpoint": "a portal — create Portal, then "
                               "stack.record(it)",
            "observability_endpoint": "the observability capability — create "
                                      "Observability, then "
                                      "stack.record(observability)",
        }
        lines = [
            f"{spec_cls.__name__} needs {field}, which nothing has published "
            f"as '{key}'. It comes from {owed_by.get(key, 'an earlier layer')}."
            for field, key in unmet
        ]
        return "\n".join(lines) + (
            f"\n\nPublished so far: {sorted(self._values)}"
        )


__all__ = ["Stack", "MissingDependencyError"]
