from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import ProtocolError

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(slots=True, frozen=True)
class RequestEnvelope:
    """Represents a transport request envelope."""

    request_id: "UUID"
    payload: bytes


@dataclass(slots=True, frozen=True)
class ResponseEnvelope:
    """Represents a transport response envelope."""

    request_id: "UUID"
    payload: bytes | None
    error: bytes | None

    def __post_init__(self) -> None:
        if (self.payload is None) == (self.error is None):
            raise ProtocolError(
                "response envelopes must include exactly one of payload or error"
            )
