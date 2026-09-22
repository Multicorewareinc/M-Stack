class NatsError(Exception):
    """Base error for NATS operations."""


class NatsDeploymentError(NatsError):
    """Raised when a NATS deployment operation fails."""


class NatsPrerequisiteError(NatsError):
    """Raised when the cluster cannot support the NATS deployment."""


class NatsTimeoutError(NatsDeploymentError):
    """Raised when NATS does not become ready within the expected time."""