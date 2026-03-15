import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

import pytest
from ociapp import (
    Application,
    OciAppServer,
    ResponseEnvelope,
    decode_request_envelope,
    encode_response_envelope,
    pack_frame,
)
from ociapp_runtime.client import execute_request
from ociapp_runtime.errors import RemoteExecutionError, ResponseProtocolError
from pydantic import BaseModel


class EchoRequest(BaseModel):
    value: str


class EchoResponse(BaseModel):
    value: str


class EchoApplication(Application[EchoRequest, EchoResponse]):
    async def execute(self, request: EchoRequest) -> EchoResponse:
        return EchoResponse(value=request.value)


class FailingApplication(Application[EchoRequest, EchoResponse]):
    async def execute(self, request: EchoRequest) -> EchoResponse:
        raise RuntimeError("boom")


class FakeReader:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed_data(self, data: bytes) -> None:
        self._buffer.extend(data)

    async def readexactly(self, count: int) -> bytes:
        if len(self._buffer) < count:
            raise asyncio.IncompleteReadError(bytes(self._buffer), count)

        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data


class FakeWriter:
    def __init__(self, reader: FakeReader, responder: "Responder") -> None:
        self._reader = reader
        self._responder = responder
        self._written = bytearray()

    def write(self, data: bytes) -> None:
        self._written.extend(data)

    async def drain(self) -> None:
        response = await self._responder(bytes(self._written))
        self._written.clear()
        self._reader.feed_data(response)

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


type Responder = Callable[[bytes], Awaitable[bytes]]
FRAME_HEADER_SIZE = 4


@pytest.mark.asyncio
async def test_execute_request_round_trips_against_ociapp_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app: Application[EchoRequest, EchoResponse] = EchoApplication()
    server = OciAppServer(app=app, socket_path=Path("/virtual/app.sock"))

    async def responder(frame: bytes) -> bytes:
        request_payload = unpack_frame(frame)
        response_payload = await server._handle_request(request_payload)
        return pack_frame(response_payload)

    reader, writer = make_fake_streams(responder)

    async def fake_open_unix_connection(path: str) -> tuple[FakeReader, FakeWriter]:
        assert path == "/virtual/app.sock"
        return reader, writer

    monkeypatch.setattr(
        "ociapp_runtime.client.asyncio.open_unix_connection", fake_open_unix_connection
    )

    result = await execute_request(Path("/virtual/app.sock"), {"value": "hello"})

    assert result == {"value": "hello"}


@pytest.mark.asyncio
async def test_execute_request_surfaces_application_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app: Application[EchoRequest, EchoResponse] = FailingApplication()
    server = OciAppServer(app=app, socket_path=Path("/virtual/app.sock"))

    async def responder(frame: bytes) -> bytes:
        request_payload = unpack_frame(frame)
        response_payload = await server._handle_request(request_payload)
        return pack_frame(response_payload)

    reader, writer = make_fake_streams(responder)

    async def fake_open_unix_connection(path: str) -> tuple[FakeReader, FakeWriter]:
        assert path == "/virtual/app.sock"
        return reader, writer

    monkeypatch.setattr(
        "ociapp_runtime.client.asyncio.open_unix_connection", fake_open_unix_connection
    )

    with pytest.raises(RemoteExecutionError) as exc_info:
        await execute_request(Path("/virtual/app.sock"), {"value": "hello"})

    assert exc_info.value.error.error_type == "RuntimeError"
    assert exc_info.value.error.message == "boom"


@pytest.mark.asyncio
async def test_execute_request_rejects_mismatched_response_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def responder(frame: bytes) -> bytes:
        request = decode_request_envelope(unpack_frame(frame))
        response_payload = encode_response_envelope(
            ResponseEnvelope(request_id=uuid4(), payload=request.payload, error=None)
        )
        return pack_frame(response_payload)

    reader, writer = make_fake_streams(responder)

    async def fake_open_unix_connection(path: str) -> tuple[FakeReader, FakeWriter]:
        assert path == "/virtual/app.sock"
        return reader, writer

    monkeypatch.setattr(
        "ociapp_runtime.client.asyncio.open_unix_connection", fake_open_unix_connection
    )

    with pytest.raises(ResponseProtocolError):
        await execute_request(Path("/virtual/app.sock"), {"value": "hello"})


def make_fake_streams(responder: Responder) -> tuple[FakeReader, FakeWriter]:
    reader = FakeReader()
    writer = FakeWriter(reader, responder)
    return reader, writer


def unpack_frame(frame: bytes) -> bytes:
    if len(frame) < FRAME_HEADER_SIZE:
        raise AssertionError("frame was missing a header")
    frame_length = int.from_bytes(frame[:FRAME_HEADER_SIZE], "big")
    payload = frame[FRAME_HEADER_SIZE:]
    if frame_length != len(payload):
        raise AssertionError("frame length did not match payload size")
    return payload
