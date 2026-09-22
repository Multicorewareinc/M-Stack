class AcceleratorError(Exception):
    """Base exception for accelerator-related operations."""


class UnsupportedAcceleratorError(AcceleratorError):
    """
    Raised when the requested accelerator is unsupported
    or is not implemented yet.
    """


class AcceleratorValidationError(AcceleratorError):
    """Raised when accelerator-specific input validation fails."""


class AcceleratorPrerequisiteError(AcceleratorError):
    """Raised when accelerator prerequisites are not satisfied."""