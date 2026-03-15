import asyncio
from typing import cast
from uuid import UUID

import msgpack

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

    return _pack_envelope({
        "request_id": str(envelope.request_id),
        "payload": envelope.payload,
    })


def decode_request_envelope(payload: bytes) -> RequestEnvelope:
    """Deserializes msgpack bytes into a request envelope."""

    envelope_data = _unpack_envelope(payload)
    request_id = _decode_request_id(envelope_data.get("request_id"))
    request_payload = _require_bytes(envelope_data.get("payload"), "payload")
    return RequestEnvelope(request_id=request_id, payload=request_payload)


def encode_response_envelope(envelope: ResponseEnvelope) -> bytes:
    """Serializes a response envelope to msgpack bytes."""

    return _pack_envelope({
        "request_id": str(envelope.request_id),
        "payload": envelope.payload,
        "error": envelope.error,
    })


def decode_response_envelope(payload: bytes) -> ResponseEnvelope:
    """Deserializes msgpack bytes into a response envelope."""

    envelope_data = _unpack_envelope(payload)
    request_id = _decode_request_id(envelope_data.get("request_id"))
    response_payload = envelope_data.get("payload")
    error_payload = envelope_data.get("error")

    if response_payload is not None and not isinstance(response_payload, bytes):
        raise ProtocolError("response payload must be bytes or null")
    if error_payload is not None and not isinstance(error_payload, bytes):
        raise ProtocolError("response error must be bytes or null")

    return ResponseEnvelope(
        request_id=request_id, payload=response_payload, error=error_payload
    )


def encode_error_payload(error: ErrorPayload) -> bytes:
    """Serializes an OCIApp error payload."""

    return cast(
        bytes,
        msgpack.packb(
            {
                "error_type": error.error_type,
                "message": error.message,
                "details": error.details,
            },
            use_bin_type=True,
        ),
    )


def decode_error_payload(payload: bytes) -> ErrorPayload:
    """Deserializes an OCIApp error payload."""

    try:
        unpacked = msgpack.unpackb(payload, raw=False)
    except (
        ValueError,
        TypeError,
        msgpack.ExtraData,
        msgpack.FormatError,
        msgpack.StackError,
    ) as exc:
        raise ProtocolError("error payload must be valid msgpack") from exc
    if not isinstance(unpacked, dict):
        raise ProtocolError("error payload must be a msgpack map")

    error_type = unpacked.get("error_type")
    message = unpacked.get("message")
    if not isinstance(error_type, str):
        raise ProtocolError("error payload error_type must be a string")
    if not isinstance(message, str):
        raise ProtocolError("error payload message must be a string")

    return ErrorPayload(
        error_type=error_type, message=message, details=unpacked.get("details")
    )


def _pack_envelope(envelope: dict[str, object]) -> bytes:
    return cast(bytes, msgpack.packb(envelope, use_bin_type=True))


def _unpack_envelope(payload: bytes) -> dict[object, object]:
    try:
        unpacked = msgpack.unpackb(payload, raw=False)
    except (
        ValueError,
        TypeError,
        msgpack.ExtraData,
        msgpack.FormatError,
        msgpack.StackError,
    ) as exc:
        raise ProtocolError("envelope payload must be valid msgpack") from exc
    if not isinstance(unpacked, dict):
        raise ProtocolError("envelope payload must be a msgpack map")

    return cast(dict[object, object], unpacked)


def _decode_request_id(value: object) -> UUID:
    if not isinstance(value, str):
        raise ProtocolError("request_id must be a UUID string")

    try:
        return UUID(value)
    except ValueError as exc:
        raise ProtocolError("request_id must be a valid UUID string") from exc


def _require_bytes(value: object, field_name: str) -> bytes:
    if not isinstance(value, bytes):
        raise ProtocolError(f"{field_name} must be bytes")

    return value
