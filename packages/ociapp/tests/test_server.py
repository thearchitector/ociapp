import asyncio
from pathlib import Path
from typing import Any, cast
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
    pack_frame,
    read_frame,
)
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


class CoordinatedApplication(Application[EchoRequest, EchoResponse]):
    def __init__(self) -> None:
        self._started_events: dict[str, asyncio.Event] = {}
        self._release_events: dict[str, asyncio.Event] = {}

    def started_event(self, value: str) -> asyncio.Event:
        return self._started_events.setdefault(value, asyncio.Event())

    def release_event(self, value: str) -> asyncio.Event:
        return self._release_events.setdefault(value, asyncio.Event())

    async def execute(self, request: EchoRequest) -> EchoResponse:
        self.started_event(request.value).set()
        await self.release_event(request.value).wait()
        return EchoResponse(value=request.value)


class CancellationAwareApplication(Application[EchoRequest, EchoResponse]):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._release = asyncio.Event()

    async def execute(self, request: EchoRequest) -> EchoResponse:
        self.started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return EchoResponse(value=request.value)


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
        self.first_write = asyncio.Event()
        self.write_count = 0

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)
        self.write_count += 1
        self.first_write.set()

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def make_envelope(value: object, request_id: object | None = None) -> bytes:
    envelope_request_id = uuid4() if request_id is None else request_id
    return encode_request_envelope(
        RequestEnvelope(
            request_id=cast("Any", envelope_request_id),
            payload=msgpack.packb(value, use_bin_type=True),
        )
    )


def make_request_frame(
    value: object, request_id: object | None = None
) -> tuple[Any, bytes]:
    envelope_request_id = uuid4() if request_id is None else request_id
    return envelope_request_id, pack_frame(
        make_envelope(value, request_id=envelope_request_id)
    )


async def decode_written_responses(payload: bytes) -> list[Any]:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    responses: list[Any] = []
    while True:
        frame = await read_frame(reader)
        if frame is None:
            return responses

        responses.append(decode_response_envelope(frame))


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    socket_path = tmp_path / "app.sock"
    socket_path.write_text("stale")
    fake_server = FakeAsyncioServer()
    captured: dict[str, str] = {}

    async def fake_start_unix_server(
        callback: object, *, path: str
    ) -> FakeAsyncioServer:
        captured["path"] = path
        await asyncio.to_thread(Path(path).touch)
        return fake_server

    monkeypatch.setattr(asyncio, "start_unix_server", fake_start_unix_server)
    server = OciAppServer(app=EchoApplication(), socket_path=socket_path)

    await server.start()

    assert socket_path.exists()
    assert socket_path.stat().st_mode & 0o777 == 0o666
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

    await server._handle_connection(reader, cast("asyncio.StreamWriter", writer))

    assert writer.closed
    assert writer.buffer == b""


@pytest.mark.asyncio
async def test_server_writes_pipelined_responses_in_completion_order() -> None:
    app = CoordinatedApplication()
    server = OciAppServer(app=app, socket_path="ignored.sock")
    reader = asyncio.StreamReader()
    writer = FakeStreamWriter()
    first_request_id, first_frame = make_request_frame({"value": "first"})
    second_request_id, second_frame = make_request_frame({"value": "second"})

    connection_task = asyncio.create_task(
        server._handle_connection(reader, cast("asyncio.StreamWriter", writer))
    )

    reader.feed_data(first_frame)
    reader.feed_data(second_frame)
    reader.feed_eof()

    await asyncio.wait_for(app.started_event("first").wait(), timeout=1)
    await asyncio.wait_for(app.started_event("second").wait(), timeout=1)

    app.release_event("second").set()
    await asyncio.wait_for(writer.first_write.wait(), timeout=1)
    app.release_event("first").set()

    await asyncio.wait_for(connection_task, timeout=1)

    responses = await decode_written_responses(bytes(writer.buffer))

    assert [response.request_id for response in responses] == [
        second_request_id,
        first_request_id,
    ]
    assert [
        msgpack.unpackb(cast("bytes", response.payload), raw=False)
        for response in responses
    ] == [{"value": "second"}, {"value": "first"}]


@pytest.mark.asyncio
async def test_server_cancels_outstanding_requests_on_protocol_failure() -> None:
    app = CancellationAwareApplication()
    server = OciAppServer(app=app, socket_path="ignored.sock")
    reader = asyncio.StreamReader()
    writer = FakeStreamWriter()
    _, request_frame = make_request_frame({"value": "hello"})
    invalid_envelope_frame = pack_frame(msgpack.packb(["invalid"], use_bin_type=True))

    connection_task = asyncio.create_task(
        server._handle_connection(reader, cast("asyncio.StreamWriter", writer))
    )

    reader.feed_data(request_frame)
    await asyncio.wait_for(app.started.wait(), timeout=1)
    reader.feed_data(invalid_envelope_frame)
    reader.feed_eof()

    await asyncio.wait_for(connection_task, timeout=1)
    await asyncio.wait_for(app.cancelled.wait(), timeout=1)

    assert writer.closed
    assert writer.write_count == 0
    assert writer.buffer == b""
