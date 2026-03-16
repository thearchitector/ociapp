import asyncio
import contextlib
from typing import TYPE_CHECKING
from uuid import uuid4

from ociapp import (
    PayloadCodecError,
    ProtocolError,
    RequestEnvelope,
    decode_error_payload,
    decode_payload,
    decode_response_envelope,
    encode_payload,
    encode_request_envelope,
    read_frame,
    write_frame,
)

from .errors import RemoteExecutionError, ResponseProtocolError

if TYPE_CHECKING:
    from pathlib import Path


async def execute_request(
    socket_path: "Path", request: dict[str, object]
) -> dict[str, object]:
    """Executes one OCIApp request against a worker socket."""

    request_id = uuid4()
    request_payload = encode_payload(request)
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except OSError as exc:
        raise ResponseProtocolError(
            f"worker socket is not available: {socket_path}"
        ) from exc

    try:
        await write_frame(
            writer,
            encode_request_envelope(
                RequestEnvelope(request_id=request_id, payload=request_payload)
            ),
        )
        frame = await read_frame(reader)
        if frame is None:
            raise ResponseProtocolError(
                "worker closed the connection before responding"
            )

        response = decode_response_envelope(frame)
        if response.request_id != request_id:
            raise ResponseProtocolError("response request_id did not match the request")
        if response.error is not None:
            raise RemoteExecutionError(decode_error_payload(response.error))
        if response.payload is None:
            raise ResponseProtocolError("worker response did not include a payload")

        return decode_payload(response.payload)
    except (PayloadCodecError, ProtocolError) as exc:
        raise ResponseProtocolError(str(exc)) from exc
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
