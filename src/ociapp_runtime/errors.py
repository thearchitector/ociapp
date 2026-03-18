from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ociapp.errors import ErrorPayload


class OCIAppRuntimeError(Exception):
    """Base exception for OCIApp runtime failures."""


class ArtifactLoadError(OCIAppRuntimeError):
    """Raised when a `.ociapp` archive cannot be loaded."""


class InstanceStartupError(OCIAppRuntimeError):
    """Raised when a worker instance does not become ready."""


class InstanceShutdownError(OCIAppRuntimeError):
    """Raised when a worker instance cannot be stopped cleanly."""


class RequestTimeoutError(OCIAppRuntimeError):
    """Raised when a request exceeds the configured timeout."""


class ResponseProtocolError(OCIAppRuntimeError):
    """Raised when a worker returns malformed or mismatched transport data."""


class RemoteExecutionError(OCIAppRuntimeError):
    """Represents a structured application error returned by OCIApp."""

    error: "ErrorPayload"

    def __init__(self, error: "ErrorPayload") -> None:
        self.error = error
        super().__init__(f"{error.error_type}: {error.message}")

    def __str__(self) -> str:
        return f"{self.error.error_type}: {self.error.message}"
