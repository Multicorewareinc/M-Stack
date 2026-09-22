class StateError(Exception):
    """Base exception for the platform state layer."""


class StateConfigurationError(StateError):
    """Invalid StateStore configuration."""


class DependencyResolutionError(StateError):
    """A deployment needs a capability nothing healthy has published."""


class DeploymentNotFoundError(StateError):
    """No deployment is recorded under the given name."""


class DeploymentConflictError(StateError):
    """A deployment already exists under the given name."""
