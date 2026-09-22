from typing import Any

from .config import ACCELERATOR_CONFIGS
from .errors import (
    AcceleratorValidationError,
    UnsupportedAcceleratorError,
)
from .models import (
    AcceleratorConfig,
    AcceleratorType,
)
from .prerequisites import (
    AcceleratorPrerequisiteManager,
)


class AcceleratorManager:
    """
    Handles accelerator-specific Helm preparation.

    Responsibilities:
    - Validate accelerator input.
    - Resolve accelerator configuration.
    - Provide the accelerator Helm chart reference.
    - Apply accelerator-specific Helm values.
    - Preserve explicit user overrides.
    - Apply namespace-specific values.
    - Validate SDK-owned prerequisites.
    """

    def __init__(
        self,
    ) -> None:

        self._prerequisites = (
            AcceleratorPrerequisiteManager()
        )

    async def prepare(
        self,
        *,
        accelerator_type: str | AcceleratorType,
        namespace: str | None,
        values: dict[str, Any],
    ) -> AcceleratorConfig:

        resolved_type = (
            self._validate_accelerator_type(
                accelerator_type
            )
        )

        config = self._resolve(
            resolved_type
        )

        if (
            resolved_type
            == AcceleratorType.TENSTORRENT
        ):
            self._prepare_tenstorrent(
                namespace=namespace,
                values=values,
            )

        await self._prerequisites.ensure(
            accelerator_type=resolved_type,
            values=values,
        )

        return config

    @staticmethod
    def _validate_accelerator_type(
        accelerator_type: str | AcceleratorType,
    ) -> AcceleratorType:

        if isinstance(
            accelerator_type,
            AcceleratorType,
        ):
            return accelerator_type

        if not isinstance(
            accelerator_type,
            str,
        ):
            raise AcceleratorValidationError(
                "accelerator_type must be a string "
                "or AcceleratorType."
            )

        accelerator_type = (
            accelerator_type.strip()
        )

        if not accelerator_type:
            raise AcceleratorValidationError(
                "accelerator_type must not be empty."
            )

        try:
            return AcceleratorType(
                accelerator_type
            )

        except ValueError as exc:
            supported = ", ".join(
                accelerator.value
                for accelerator
                in AcceleratorType
            )

            raise UnsupportedAcceleratorError(
                f"Unsupported accelerator type "
                f"'{accelerator_type}'. "
                f"Supported accelerator types: "
                f"{supported}."
            ) from exc

    @staticmethod
    def _resolve(
        accelerator_type: AcceleratorType,
    ) -> AcceleratorConfig:

        config = ACCELERATOR_CONFIGS.get(
            accelerator_type
        )

        if config is None:
            raise UnsupportedAcceleratorError(
                f"Accelerator "
                f"'{accelerator_type.value}' "
                f"is recognized but is not "
                f"implemented yet."
            )

        return config

    @staticmethod
    def _prepare_tenstorrent(
        *,
        namespace: str | None,
        values: dict[str, Any],
    ) -> None:

        if (
            not namespace
            or not namespace.strip()
        ):
            raise AcceleratorValidationError(
                "namespace is required for "
                "Tenstorrent installation."
            )

        namespace = namespace.strip()

        AcceleratorManager._set_default_enabled(
            values=values,
            component="node-feature-discovery",
            enabled=True,
        )

        AcceleratorManager._set_default_enabled(
            values=values,
            component="tt-k8s-driver-manager",
            enabled=True,
        )

        telemetry_values = (
            AcceleratorManager
            ._set_default_enabled(
                values=values,
                component="tt-telemetry",
                enabled=False,
            )
        )

        AcceleratorManager._set_default_enabled(
            values=values,
            component="tt-fabric-manager",
            enabled=True,
        )

        AcceleratorManager._set_default_enabled(
            values=values,
            component="tt-dra-driver",
            enabled=True,
        )

        AcceleratorManager._set_default_enabled(
            values=values,
            component="jobset",
            enabled=False,
        )

        AcceleratorManager._set_default_enabled(
            values=values,
            component="kubepmix",
            enabled=False,
        )

        # Tenstorrent telemetry explicitly uses
        # the user-provided Helm namespace.
        telemetry_values[
            "namespace"
        ] = namespace

    @staticmethod
    def _set_default_enabled(
        *,
        values: dict[str, Any],
        component: str,
        enabled: bool,
    ) -> dict[str, Any]:

        component_values = values.setdefault(
            component,
            {},
        )

        if not isinstance(
            component_values,
            dict,
        ):
            raise AcceleratorValidationError(
                f"Helm value '{component}' "
                f"must be a dictionary."
            )

        component_values.setdefault(
            "enabled",
            enabled,
        )

        return component_values