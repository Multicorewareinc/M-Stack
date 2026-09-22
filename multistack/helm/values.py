from typing import Any, List

from .errors import HelmValidationError


class HelmValuesValidator:
    """
    Checks user-provided Helm values against the chart's values.yaml.

    Helm silently ignores a value that doesn't exist, which makes a typo
    indistinguishable from a setting that didn't take. Comparing supplied
    keys against the chart's declared defaults catches that.

    Not strict by default, and that isn't timidity. A chart can legitimately
    read a key it never declares — `.Values.foo | default`, `global.*` on a
    chart that doesn't list it, subchart aliases. The MinIO tenant chart
    documents `.tenant.configuration.name` in its own values.yaml and reads
    it in two templates, while the parsed defaults have no `configuration`
    key at all: strict mode rejects a value the chart supports. So the
    default is to report unknown paths and let the caller decide, which is
    the same shape `check_prerequisites` uses elsewhere in this SDK —
    warnings returned, hard failures raised.

    It checks that a path exists. It does not check types or allowed values.
    """

    @classmethod
    def check(
        cls,
        user_values: dict[str, Any],
        default_values: dict[str, Any],
    ) -> List[str]:
        """Unknown value paths, as human-readable strings. Never raises for
        an unknown path — that is `validate(strict=True)`."""
        unknown: List[str] = []
        try:
            cls.validate(user_values, default_values, strict=True)
        except HelmValidationError as exc:
            unknown.append(str(exc))
        return unknown

    @classmethod
    def validate(
        cls,
        user_values: dict[str, Any],
        default_values: dict[str, Any],
        strict: bool = False,
    ) -> List[str]:
        """
        Validate user-provided values against chart default values.

        Example:

            user_values:
                {
                    "service": {
                        "type": "ClusterIP"
                    }
                }

            default_values:
                {
                    "service": {
                        "type": "LoadBalancer",
                        ...
                    }
                }

        Returns the unknown paths it found. With `strict=True` the first
        one raises `HelmValidationError` instead.

        Raises:
            HelmValidationError:
                Always for a malformed argument; for an unknown key/path
                only when `strict` is set.
        """

        if not isinstance(user_values, dict):
            raise HelmValidationError(
                "Helm values must be provided as a dictionary."
            )

        if not isinstance(default_values, dict):
            raise HelmValidationError(
                "Chart default values must be a dictionary."
            )

        unknown: List[str] = []
        cls._validate_mapping(
            user_values=user_values,
            default_values=default_values,
            path="",
            unknown=unknown,
            strict=strict,
        )
        return unknown

    @classmethod
    def _validate_mapping(
        cls,
        *,
        user_values: dict[str, Any],
        default_values: dict[str, Any],
        path: str,
        unknown: List[str],
        strict: bool,
    ) -> None:
        for key, user_value in user_values.items():

            current_path = (
                f"{path}.{key}"
                if path
                else key
            )

            if key not in default_values:
                message = (
                    f"Unsupported Helm value: '{current_path}'. The chart's "
                    "values.yaml does not declare it — which is usually a "
                    "typo, but some charts read values they never declare."
                )
                if strict:
                    raise HelmValidationError(message)
                unknown.append(message)
                continue

            default_value = default_values[key]

            if (
                isinstance(user_value, dict)
                and isinstance(default_value, dict)
            ):
                # Some Helm values intentionally use an empty mapping
                # so users can supply arbitrary child keys.
                #
                # Examples:
                #
                # podLabels: {}
                # podAnnotations: {}
                # nodeSelector: {}
                #
                # Therefore, child keys under an empty default
                # dictionary are allowed.
                if not default_value:
                    continue

                cls._validate_mapping(
                    user_values=user_value,
                    default_values=default_value,
                    path=current_path,
                    unknown=unknown,
                    strict=strict,
                )