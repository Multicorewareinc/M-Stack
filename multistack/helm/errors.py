class HelmManagerError(Exception):
    """Base exception for Helm integration layer."""


class HelmConfigurationError(HelmManagerError):
    """Invalid HelmManager configuration."""


class HelmValidationError(HelmManagerError):
    """Invalid input provided to HelmManager."""


class HelmInstallError(HelmManagerError):
    """Helm install operation failed."""


class HelmUpgradeError(HelmManagerError):
    """Helm upgrade operation failed."""


class HelmDeploymentError(HelmManagerError):
    """Helm install or upgrade operation failed."""


class HelmUninstallError(HelmManagerError):
    """Helm uninstall operation failed."""


class HelmReleaseNotFoundError(HelmManagerError):
    """Requested Helm release was not found."""


class HelmOperationCancelledError(HelmManagerError):
    """Helm operation was cancelled."""


class HelmTimeoutError(HelmManagerError):
    """Helm operation timed out."""


class HelmChartNotFoundError(HelmManagerError):
    """Chart was not found in supported repositories."""


class HelmChartVersionNotFoundError(HelmManagerError):
    """Requested chart version was not found."""


class HelmRepositoryError(HelmManagerError):
    """Failed while accessing Helm repository."""