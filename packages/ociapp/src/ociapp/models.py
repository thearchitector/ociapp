from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

if TYPE_CHECKING:
    from typing import Self


class _RequestEnvelope(BaseModel):
    """Represents a transport request envelope."""

    model_config = ConfigDict(frozen=True)

    request_id: UUID
    payload: bytes

    @field_validator("payload", mode="before")
    @classmethod
    def _validate_payload_bytes(cls, value: object) -> object:
        if not isinstance(value, bytes):
            raise ValueError("payload must be bytes")

        return value


class _ResponseEnvelope(BaseModel):
    """Represents a transport response envelope."""

    model_config = ConfigDict(frozen=True)

    request_id: UUID
    payload: bytes | None = None
    error: bytes | None = None

    @field_validator("payload", "error", mode="before")
    @classmethod
    def _validate_transport_bytes(cls, value: object) -> object:
        if value is not None and not isinstance(value, bytes):
            raise ValueError("payload and error must be bytes or null")

        return value

    @model_validator(mode="after")
    def _validate_payload_or_error(self) -> "Self":
        if (self.payload is None) == (self.error is None):
            raise ValueError(
                "response envelopes must include exactly one of payload or error"
            )

        return self
