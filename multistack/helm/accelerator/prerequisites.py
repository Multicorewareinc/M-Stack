from typing import Any

from .models import AcceleratorType


class AcceleratorPrerequisiteManager:
    """
    Handles prerequisites owned by the SDK.

    Host-level accelerator driver installation and lifecycle
    management are currently the user's responsibility.
    """

    async def ensure(
        self,
        *,
        accelerator_type: AcceleratorType,
        values: dict[str, Any],
    ) -> None:

        if accelerator_type == AcceleratorType.TENSTORRENT:
            await self._ensure_tenstorrent(
                values=values
            )

    async def _ensure_tenstorrent(
        self,
        *,
        values: dict[str, Any],
    ) -> None:

        if self._is_enabled(
            values,
            "kubepmix",
        ):
            await self._ensure_cert_manager()

        if self._is_enabled(
            values,
            "tt-dra-driver",
        ):
            await self._ensure_dra_prerequisites()

        if self._is_enabled(
            values,
            "tt-k8s-driver-manager",
        ):
            await self._ensure_driver_manager_prerequisites()

    async def _ensure_cert_manager(
        self,
    ) -> None:
        """
        Placeholder for cert-manager prerequisite handling.
        """
        return

    async def _ensure_dra_prerequisites(
        self,
    ) -> None:
        """
        Placeholder for DRA prerequisite handling.
        """
        return

    async def _ensure_driver_manager_prerequisites(
        self,
    ) -> None:
        """
        Placeholder for TT Driver Manager prerequisite handling.
        """
        return

    @staticmethod
    def _is_enabled(
        values: dict[str, Any],
        component: str,
    ) -> bool:

        component_values = values.get(
            component,
            {},
        )

        if not isinstance(
            component_values,
            dict,
        ):
            return False

        return bool(
            component_values.get(
                "enabled",
                False,
            )
        )