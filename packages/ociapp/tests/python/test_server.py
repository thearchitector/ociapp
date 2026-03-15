import asyncio
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import msgpack
import pytest
from ociapp import (
    Application,
    OciAppServer,
    RequestEnvelope,
    decode_error_payload,
    decode_response_envelope,
    encode_request_envelope,
)
from pydantic import BaseModel

if TYPE_CHECKING:
    from pathlib import Path


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


class FakeAsyncioServer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    async def serve_forever(self) -> None:
        raise AssertionError("serve_forever should not be called in this test")


class FakeStreamWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def make_envelope(value: object) -> bytes:
    return encode_request_envelope(
        RequestEnvelope(
            request_id=uuid4(), payload=msgpack.packb(value, use_bin_type=True)
        )
    )


@pytest.mark.asyncio
async def test_server_round_trip() -> None:
    server = OciAppServer(app=EchoApplication(), socket_path="ignored.sock")

    response_frame = await server._handle_request(make_envelope({"value": "hello"}))

    response = decode_response_envelope(response_frame)
    assert response.payload is not None
    assert msgpack.unpackb(response.payload, raw=False) == {"value": "hello"}


@pytest.mark.asyncio
async def test_server_returns_validation_error() -> None:
    server = OciAppServer(app=EchoApplication(), socket_path="ignored.sock")

    response_frame = await server._handle_request(make_envelope({"missing": "value"}))

    response = decode_response_envelope(response_frame)
    assert response.error is not None
    error = decode_error_payload(response.error)
    assert error.error_type == "ValidationError"


@pytest.mark.asyncio
async def test_server_maps_application_exception() -> None:
    server = OciAppServer(app=FailingApplication(), socket_path="ignored.sock")

    response_frame = await server._handle_request(make_envelope({"value": "hello"}))

    response = decode_response_envelope(response_frame)
    assert response.error is not None
    error = decode_error_payload(response.error)
    assert error.error_type == "RuntimeError"
    assert error.message == "boom"


@pytest.mark.asyncio
async def test_server_replaces_stale_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Path"
) -> None:
    socket_path = tmp_path / "app.sock"
    socket_path.write_text("stale")
    fake_server = FakeAsyncioServer()
    captured: dict[str, str] = {}

    async def fake_start_unix_server(
        callback: object, *, path: str
    ) -> FakeAsyncioServer:
        captured["path"] = path
        return fake_server

    monkeypatch.setattr(asyncio, "start_unix_server", fake_start_unix_server)
    server = OciAppServer(app=EchoApplication(), socket_path=socket_path)

    await server.start()

    assert not socket_path.exists()
    assert captured["path"] == str(socket_path)

    socket_path.write_text("placeholder")
    await server.close()

    assert fake_server.closed
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_server_rejects_malformed_frame() -> None:
    server = OciAppServer(app=EchoApplication(), socket_path="ignored.sock")
    reader = asyncio.StreamReader()
    reader.feed_data((0).to_bytes(4, "big"))
    reader.feed_eof()
    writer = FakeStreamWriter()

    await server._handle_connection(reader, cast(asyncio.StreamWriter, writer))

    assert writer.closed
    assert writer.buffer == b""
