from uuid import uuid4

import msgpack
import pytest
from ociapp.errors import ErrorPayload, ProtocolError
from ociapp.models import _RequestEnvelope, _ResponseEnvelope
from ociapp.protocol import (
    decode_error_payload,
    decode_payload,
    decode_request_envelope,
    decode_response_envelope,
    encode_error_payload,
    encode_payload,
    encode_request_envelope,
    encode_response_envelope,
    pack_frame,
    read_frame,
)
from pydantic import ValidationError


@pytest.mark.asyncio
async def test_read_frame_round_trip() -> None:
    reader = pytest.importorskip("asyncio").StreamReader()
    frame = pack_frame(b"payload")
    reader.feed_data(frame)
    reader.feed_eof()

    assert await read_frame(reader) == b"payload"
    assert await read_frame(reader) is None


@pytest.mark.asyncio
async def test_read_frame_rejects_truncated_body() -> None:
    reader = pytest.importorskip("asyncio").StreamReader()
    reader.feed_data((5).to_bytes(4, "big") + b"abc")
    reader.feed_eof()

    with pytest.raises(ProtocolError, match="frame body"):
        await read_frame(reader)


def test_request_envelope_round_trip() -> None:
    request_id = uuid4()
    encoded = encode_request_envelope(
        _RequestEnvelope(request_id=request_id, payload=b"abc")
    )

    decoded = decode_request_envelope(encoded)

    assert decoded == _RequestEnvelope(request_id=request_id, payload=b"abc")


def test_response_envelope_round_trip() -> None:
    request_id = uuid4()
    encoded = encode_response_envelope(
        _ResponseEnvelope(request_id=request_id, payload=b"abc", error=None)
    )

    decoded = decode_response_envelope(encoded)

    assert decoded == _ResponseEnvelope(
        request_id=request_id, payload=b"abc", error=None
    )


@pytest.mark.parametrize(
    ("payload", "error"),
    [(None, None), (b"abc", b"boom")],
    ids=["missing-both", "present-both"],
)
def test_response_envelope_validation_rejects_invalid_payload_error_combinations(
    payload: bytes | None, error: bytes | None
) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        _ResponseEnvelope(request_id=uuid4(), payload=payload, error=error)


def test_error_payload_round_trip() -> None:
    encoded = encode_error_payload(
        ErrorPayload(
            error_type="BoomError", message="bad request", details={"retryable": False}
        )
    )

    decoded = decode_error_payload(encoded)

    assert decoded == ErrorPayload(
        error_type="BoomError", message="bad request", details={"retryable": False}
    )


def test_payload_round_trip() -> None:
    encoded = encode_payload({"value": "hello"})

    assert decode_payload(encoded) == {"value": "hello"}


def test_decode_request_envelope_rejects_invalid_request_id() -> None:
    with pytest.raises(ProtocolError, match="valid UUID"):
        decode_request_envelope(
            msgpack.packb(
                {"request_id": "not-a-uuid", "payload": b"abc"}, use_bin_type=True
            )
        )


@pytest.mark.parametrize(
    ("payload", "pattern"),
    [
        (msgpack.packb({"request_id": str(uuid4())}, use_bin_type=True), "exactly one"),
        (
            msgpack.packb(
                {"request_id": str(uuid4()), "payload": "abc", "error": None},
                use_bin_type=True,
            ),
            "bytes or null",
        ),
    ],
    ids=["missing-both", "payload-string"],
)
def test_decode_response_envelope_maps_validation_failures_to_protocol_error(
    payload: bytes, pattern: str
) -> None:
    with pytest.raises(ProtocolError, match=pattern):
        decode_response_envelope(payload)


def test_decode_error_payload_maps_validation_failures_to_protocol_error() -> None:
    with pytest.raises(ProtocolError, match="message"):
        decode_error_payload(
            msgpack.packb(
                {"error_type": "BoomError", "message": 123}, use_bin_type=True
            )
        )
