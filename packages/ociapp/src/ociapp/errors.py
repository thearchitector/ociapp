from pydantic import BaseModel, ConfigDict


class OCIAppError(Exception):
    """Base exception for OCIApp failures."""


class ProtocolError(OCIAppError):
    """Raised when transport protocol handling fails."""


class FrameError(ProtocolError):
    """Raised when framed transport data is malformed."""


class EnvelopeError(ProtocolError):
    """Raised when an envelope cannot be decoded or validated."""


class PayloadCodecError(OCIAppError):
    """Raised when a payload cannot be encoded or decoded."""


class ApplicationLoadError(OCIAppError):
    """Raised when an application descriptor cannot be imported."""


class ServerLifecycleError(OCIAppError):
    """Raised when the server cannot manage its socket path."""


class ErrorPayload(BaseModel):
    """Describes a structured application error payload."""

    model_config = ConfigDict(frozen=True)

    error_type: str
    message: str
    details: object | None = None
