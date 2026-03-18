import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid4

import msgpack
import pytest
from ociapp import Application
from ociapp.errors import ErrorPayload
from ociapp.models import _RequestEnvelope, _ResponseEnvelope
from ociapp.protocol import (
    decode_payload,
    decode_request_envelope,
    encode_error_payload,
    encode_payload,
    encode_response_envelope,
    pack_frame,
)
from ociapp.server import _OciAppServer
from pydantic import BaseModel

from ociapp_runtime.client import _execute_request, _open_worker_session, _WorkerSession
from ociapp_runtime.errors import (
    RemoteExecutionError,
    RequestTimeoutError,
    ResponseProtocolError,
)


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


type Responder = Callable[[bytes], Awaitable[None]]


class FakeStreamWriter:
    def __init__(self, reader: asyncio.StreamReader, responder: Responder) -> None:
        self._reader = reader
        self._responder = responder
        self._written = bytearray()

    def write(self, data: bytes) -> None:
        self._written.extend(data)

    async def drain(self) -> None:
        frame = bytes(self._written)
        self._written.clear()
        await self._responder(frame)

    def close(self) -> None:
        self._reader.feed_eof()

    async def wait_closed(self) -> None:
        return None


class RecordingTransport:
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.requests: list[_RequestEnvelope] = []
        self._request_condition = asyncio.Condition()
        self.writer = FakeStreamWriter(self.reader, self._handle_write)

    async def _handle_write(self, frame: bytes) -> None:
        async with self._request_condition:
            self.requests.append(decode_request_envelope(unpack_frame(frame)))
            self._request_condition.notify_all()

    async def wait_for_requests(self, count: int) -> None:
        async with self._request_condition:
            await self._request_condition.wait_for(lambda: len(self.requests) >= count)

    def feed_success(self, request_id: UUID, payload: dict[str, object]) -> None:
        self.feed_response(
            encode_response_envelope(
                _ResponseEnvelope(
                    request_id=request_id, payload=encode_payload(payload), error=None
                )
            )
        )

    def feed_error(self, request_id: UUID, error: ErrorPayload) -> None:
        self.feed_response(
            encode_response_envelope(
                _ResponseEnvelope(
                    request_id=request_id,
                    payload=None,
                    error=encode_error_payload(error),
                )
            )
        )

    def feed_response(self, response_payload: bytes) -> None:
        self.reader.feed_data(pack_frame(response_payload))

    def feed_eof(self) -> None:
        self.reader.feed_eof()

    def request_id_for_value(self, value: str) -> UUID:
        for request in self.requests:
            payload = decode_payload(request.payload)
            if payload["value"] == value:
                return request.request_id

        raise AssertionError(f"request for value {value!r} was not recorded")


def feed_truncated_frame(transport: RecordingTransport) -> None:
    transport.reader.feed_data((8).to_bytes(4, "big") + b"bad")
    transport.feed_eof()


@pytest.mark.asyncio
async def test_execute_request_round_trips_against_ociapp_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app: Application[EchoRequest, EchoResponse] = EchoApplication()
    server = _OciAppServer(app=app, socket_path=Path("/virtual/app.sock"))
    reader = asyncio.StreamReader()

    async def responder(frame: bytes) -> None:
        request_payload = unpack_frame(frame)
        response_payload = await server._handle_request(request_payload)
        reader.feed_data(pack_frame(response_payload))

    writer = FakeStreamWriter(reader, responder)

    async def fake_open_unix_connection(
        path: str,
    ) -> tuple[asyncio.StreamReader, FakeStreamWriter]:
        assert path == "/virtual/app.sock"
        return reader, writer

    monkeypatch.setattr(
        "ociapp_runtime.client.asyncio.open_unix_connection", fake_open_unix_connection
    )

    result = await _execute_request(Path("/virtual/app.sock"), {"value": "hello"})

    assert result == {"value": "hello"}


@pytest.mark.asyncio
async def test_execute_request_surfaces_application_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app: Application[EchoRequest, EchoResponse] = FailingApplication()
    server = _OciAppServer(app=app, socket_path=Path("/virtual/app.sock"))
    reader = asyncio.StreamReader()

    async def responder(frame: bytes) -> None:
        request_payload = unpack_frame(frame)
        response_payload = await server._handle_request(request_payload)
        reader.feed_data(pack_frame(response_payload))

    writer = FakeStreamWriter(reader, responder)

    async def fake_open_unix_connection(
        path: str,
    ) -> tuple[asyncio.StreamReader, FakeStreamWriter]:
        assert path == "/virtual/app.sock"
        return reader, writer

    monkeypatch.setattr(
        "ociapp_runtime.client.asyncio.open_unix_connection", fake_open_unix_connection
    )

    with pytest.raises(RemoteExecutionError) as exc_info:
        await _execute_request(Path("/virtual/app.sock"), {"value": "hello"})

    assert exc_info.value.error.error_type == "RuntimeError"
    assert exc_info.value.error.message == "boom"


@pytest.mark.asyncio
async def test_worker_session_drops_unknown_response_ids_without_retiring_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordingTransport()
    session = await open_session(monkeypatch, transport)
    reader_task = asyncio.create_task(session.read_responses())

    try:
        request_task = asyncio.create_task(
            session.execute({"value": "hello"}, request_timeout=0.2)
        )
        await transport.wait_for_requests(1)

        transport.feed_success(uuid4(), {"value": "ignored"})
        transport.feed_success(
            transport.request_id_for_value("hello"), {"value": "hello"}
        )

        assert await request_task == {"value": "hello"}
        assert session.is_open
    finally:
        await close_session(session, reader_task)


@pytest.mark.asyncio
async def test_worker_session_times_out_one_request_without_affecting_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordingTransport()
    session = await open_session(monkeypatch, transport)
    reader_task = asyncio.create_task(session.read_responses())

    try:
        slow_task = asyncio.create_task(
            session.execute({"value": "slow"}, request_timeout=0.05)
        )
        fast_task = asyncio.create_task(
            session.execute({"value": "fast"}, request_timeout=0.2)
        )
        await transport.wait_for_requests(2)

        transport.feed_success(
            transport.request_id_for_value("fast"), {"value": "fast"}
        )

        assert await fast_task == {"value": "fast"}
        with pytest.raises(RequestTimeoutError):
            await slow_task

        transport.feed_success(
            transport.request_id_for_value("slow"), {"value": "slow"}
        )

        third_task = asyncio.create_task(
            session.execute({"value": "third"}, request_timeout=0.2)
        )
        await transport.wait_for_requests(3)
        transport.feed_success(
            transport.request_id_for_value("third"), {"value": "third"}
        )

        assert await third_task == {"value": "third"}
        assert session.is_open
    finally:
        await close_session(session, reader_task)


@pytest.mark.asyncio
async def test_worker_session_invalid_inner_payload_fails_only_matching_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = RecordingTransport()
    session = await open_session(monkeypatch, transport)
    reader_task = asyncio.create_task(session.read_responses())

    try:
        broken_task = asyncio.create_task(
            session.execute({"value": "broken"}, request_timeout=0.2)
        )
        ok_task = asyncio.create_task(
            session.execute({"value": "ok"}, request_timeout=0.2)
        )
        await transport.wait_for_requests(2)

        transport.feed_response(
            encode_response_envelope(
                _ResponseEnvelope(
                    request_id=transport.request_id_for_value("broken"),
                    payload=msgpack.packb("bad", use_bin_type=True),
                    error=None,
                )
            )
        )
        transport.feed_success(transport.request_id_for_value("ok"), {"value": "ok"})

        with pytest.raises(ResponseProtocolError):
            await broken_task
        assert await ok_task == {"value": "ok"}
        assert session.is_open
    finally:
        await close_session(session, reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inject_failure", "message"),
    [
        pytest.param(
            lambda transport: transport.feed_eof(),
            "worker closed the connection before responding",
            id="eof",
        ),
        pytest.param(
            feed_truncated_frame,
            "unexpected EOF while reading frame body",
            id="truncated",
        ),
        pytest.param(
            lambda transport: transport.feed_response(
                msgpack.packb(["bad-envelope"], use_bin_type=True)
            ),
            "envelope payload must be a msgpack map",
            id="bad-envelope",
        ),
    ],
)
async def test_worker_session_fatal_transport_failure_fails_all_pending_requests(
    monkeypatch: pytest.MonkeyPatch,
    inject_failure: Callable[[RecordingTransport], None],
    message: str,
) -> None:
    transport = RecordingTransport()
    session = await open_session(monkeypatch, transport)
    reader_task = asyncio.create_task(session.read_responses())

    first_task = asyncio.create_task(
        session.execute({"value": "first"}, request_timeout=1.0)
    )
    second_task = asyncio.create_task(
        session.execute({"value": "second"}, request_timeout=1.0)
    )
    await transport.wait_for_requests(2)

    inject_failure(transport)

    with pytest.raises(ResponseProtocolError, match=message):
        await reader_task
    with pytest.raises(ResponseProtocolError, match=message):
        await first_task
    with pytest.raises(ResponseProtocolError, match=message):
        await second_task
    assert not session.is_open
    assert session.fatal_error is not None


async def open_session(
    monkeypatch: pytest.MonkeyPatch, transport: RecordingTransport
) -> _WorkerSession:
    async def fake_open_unix_connection(
        path: str,
    ) -> tuple[asyncio.StreamReader, FakeStreamWriter]:
        assert path == "/virtual/app.sock"
        return transport.reader, transport.writer

    monkeypatch.setattr(
        "ociapp_runtime.client.asyncio.open_unix_connection", fake_open_unix_connection
    )
    return await _open_worker_session(Path("/virtual/app.sock"))


async def close_session(
    session: _WorkerSession, reader_task: asyncio.Task[None]
) -> None:
    await session.close()
    with contextlib.suppress(ResponseProtocolError):
        await reader_task


def unpack_frame(frame: bytes) -> bytes:
    frame_length = int.from_bytes(frame[:4], "big")
    payload = frame[4:]
    if frame_length != len(payload):
        raise AssertionError("frame length did not match payload size")
    return payload
