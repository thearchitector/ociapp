import asyncio
from typing import cast

import msgpack
from pydantic import ValidationError

from .errors import ErrorPayload, ProtocolError
from .models import RequestEnvelope, ResponseEnvelope

__all__ = [
    "DEFAULT_SOCKET_PATH",
    "FRAME_HEADER_SIZE",
    "ErrorPayload",
    "ProtocolError",
    "RequestEnvelope",
    "ResponseEnvelope",
    "decode_error_payload",
    "decode_request_envelope",
    "decode_response_envelope",
    "encode_error_payload",
    "encode_request_envelope",
    "encode_response_envelope",
    "pack_frame",
    "read_frame",
    "write_frame",
]


DEFAULT_SOCKET_PATH = "/run/ociapp/app.sock"
FRAME_HEADER_SIZE = 4


def pack_frame(payload: bytes) -> bytes:
    """Prefixes a payload with the OCIApp frame header."""

    if not payload:
        raise ProtocolError("frame payload must not be empty")

    length = len(payload)
    return length.to_bytes(FRAME_HEADER_SIZE, "big") + payload


async def write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    """Writes a framed payload to a stream."""

    writer.write(pack_frame(payload))
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """Reads a single framed payload from a stream."""

    try:
        header = await reader.readexactly(FRAME_HEADER_SIZE)
    except asyncio.IncompleteReadError as exc:
        if exc.partial == b"":
            return None

        raise ProtocolError("unexpected EOF while reading frame header") from exc

    frame_length = int.from_bytes(header, "big")
    if frame_length <= 0:
        raise ProtocolError("frame length must be positive")

    try:
        return await reader.readexactly(frame_length)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("unexpected EOF while reading frame body") from exc


def encode_request_envelope(envelope: RequestEnvelope) -> bytes:
    """Serializes a request envelope to msgpack bytes."""

    request_data = envelope.model_dump(mode="python")
    request_data["request_id"] = str(envelope.request_id)
    return _pack_map(request_data)


def decode_request_envelope(payload: bytes) -> RequestEnvelope:
    """Deserializes msgpack bytes into a request envelope."""

    try:
        return RequestEnvelope.model_validate(
            _unpack_map(payload, label="envelope payload")
        )
    except ValidationError as exc:
        raise _protocol_validation_error("request envelope", exc) from exc


def encode_response_envelope(envelope: ResponseEnvelope) -> bytes:
    """Serializes a response envelope to msgpack bytes."""

    response_data = envelope.model_dump(mode="python")
    response_data["request_id"] = str(envelope.request_id)
    return _pack_map(response_data)


def decode_response_envelope(payload: bytes) -> ResponseEnvelope:
    """Deserializes msgpack bytes into a response envelope."""

    try:
        return ResponseEnvelope.model_validate(
            _unpack_map(payload, label="envelope payload")
        )
    except ValidationError as exc:
        raise _protocol_validation_error("response envelope", exc) from exc


def encode_error_payload(error: ErrorPayload) -> bytes:
    """Serializes an OCIApp error payload."""

    return _pack_map(error.model_dump(mode="python"))


def decode_error_payload(payload: bytes) -> ErrorPayload:
    """Deserializes an OCIApp error payload."""

    try:
        return ErrorPayload.model_validate(_unpack_map(payload, label="error payload"))
    except ValidationError as exc:
        raise _protocol_validation_error("error payload", exc) from exc


def _pack_map(payload: dict[str, object]) -> bytes:
    return cast("bytes", msgpack.packb(payload, use_bin_type=True))


def _unpack_map(payload: bytes, *, label: str) -> dict[object, object]:
    try:
        unpacked = msgpack.unpackb(payload, raw=False)
    except (
        ValueError,
        TypeError,
        msgpack.ExtraData,
        msgpack.FormatError,
        msgpack.StackError,
    ) as exc:
        raise ProtocolError(f"{label} must be valid msgpack") from exc
    if not isinstance(unpacked, dict):
        raise ProtocolError(f"{label} must be a msgpack map")

    return cast("dict[object, object]", unpacked)


def _protocol_validation_error(label: str, error: ValidationError) -> ProtocolError:
    details = "; ".join(_format_validation_error(item) for item in error.errors())
    return ProtocolError(f"{label} is invalid: {details}")


def _format_validation_error(error: object) -> str:
    error_details = cast("dict[str, object]", error)
    location = ".".join(
        str(part) for part in cast("tuple[object, ...]", error_details["loc"])
    )
    message = cast("str", error_details["msg"])
    if not location:
        return message

    return f"{location}: {message}"
