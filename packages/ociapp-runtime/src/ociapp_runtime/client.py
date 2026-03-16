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

from .errors import (
    OCIAppRuntimeError,
    RemoteExecutionError,
    RequestTimeoutError,
    ResponseProtocolError,
)

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID


class WorkerSession:
    """Manages one persistent worker socket with multiplexed request futures."""

    def __init__(
        self,
        socket_path: "Path",
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._socket_path = socket_path
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._failure_lock = asyncio.Lock()
        self._pending: dict["UUID", asyncio.Future[dict[str, object]]] = {}
        self._close_requested = False
        self._fatal_error: ResponseProtocolError | None = None

    @property
    def is_open(self) -> bool:
        """Returns whether the session can still accept new requests."""

        return self._fatal_error is None and not self._close_requested

    @property
    def fatal_error(self) -> ResponseProtocolError | None:
        """Returns the fatal transport error, when one has been recorded."""

        return self._fatal_error

    async def execute(
        self, request: dict[str, object], *, request_timeout: float | None = None
    ) -> dict[str, object]:
        """Writes one request and waits for its correlated response."""

        if self._fatal_error is not None:
            raise self._fatal_error
        if self._close_requested:
            raise OCIAppRuntimeError("worker session is closing")

        request_id = uuid4()
        request_payload = encode_payload(request)
        response_future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = response_future

        try:
            async with self._write_lock:
                if self._fatal_error is not None:
                    raise self._fatal_error
                if self._close_requested:
                    raise OCIAppRuntimeError("worker session is closing")

                try:
                    await write_frame(
                        self._writer,
                        encode_request_envelope(
                            RequestEnvelope(
                                request_id=request_id, payload=request_payload
                            )
                        ),
                    )
                except (ConnectionError, OSError) as exc:
                    raise await self._fail_transport(
                        ResponseProtocolError(
                            f"worker socket is not available: {self._socket_path}"
                        )
                    ) from exc

            if request_timeout is None:
                return await response_future

            try:
                async with asyncio.timeout(request_timeout):
                    return await asyncio.shield(response_future)
            except TimeoutError as exc:
                pending_future = self._pending.pop(request_id, None)
                if pending_future is response_future:
                    response_future.cancel()
                    raise RequestTimeoutError(
                        f"request timed out after {request_timeout}s"
                    ) from exc
                return await response_future
        except asyncio.CancelledError:
            pending_future = self._pending.pop(request_id, None)
            if pending_future is response_future:
                response_future.cancel()
            raise
        except BaseException:
            pending_future = self._pending.pop(request_id, None)
            if pending_future is response_future and not response_future.done():
                response_future.cancel()
            raise

    async def read_responses(self) -> None:
        """Continuously reads responses and resolves pending request futures."""

        while True:
            try:
                frame = await read_frame(self._reader)
            except ProtocolError as exc:
                raise await self._fail_transport(
                    ResponseProtocolError(str(exc))
                ) from exc
            except (ConnectionError, OSError) as exc:
                raise await self._fail_transport(
                    ResponseProtocolError(
                        f"worker socket is not available: {self._socket_path}"
                    )
                ) from exc

            if frame is None:
                if self._fatal_error is None and self._close_requested:
                    return
                raise await self._fail_transport(
                    ResponseProtocolError(
                        "worker closed the connection before responding"
                    )
                )

            try:
                response = decode_response_envelope(frame)
            except ProtocolError as exc:
                raise await self._fail_transport(
                    ResponseProtocolError(str(exc))
                ) from exc

            pending_future = self._pending.pop(response.request_id, None)
            if pending_future is None or pending_future.done():
                continue

            try:
                if response.error is not None:
                    pending_future.set_exception(
                        RemoteExecutionError(decode_error_payload(response.error))
                    )
                    continue
                if response.payload is None:
                    raise ResponseProtocolError(
                        "worker response did not include a payload"
                    )

                pending_future.set_result(decode_payload(response.payload))
            except (PayloadCodecError, ProtocolError) as exc:
                pending_future.set_exception(ResponseProtocolError(str(exc)))
            except BaseException as exc:  # pragma: no cover - defensive fallback
                pending_future.set_exception(exc)

    async def close(self, error: BaseException | None = None) -> None:
        """Closes the worker socket and optionally fails pending requests."""

        self._close_requested = True
        if error is not None:
            self._fail_pending(error)

        self._writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self._writer.wait_closed()

    async def _fail_transport(
        self, error: ResponseProtocolError
    ) -> ResponseProtocolError:
        async with self._failure_lock:
            if self._fatal_error is not None:
                return self._fatal_error

            self._fatal_error = error
            self._fail_pending(error)
            self._writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await self._writer.wait_closed()
            return error

    def _fail_pending(self, error: BaseException) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)


async def open_worker_session(socket_path: "Path") -> WorkerSession:
    """Opens a persistent client session to a worker socket."""

    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except OSError as exc:
        raise ResponseProtocolError(
            f"worker socket is not available: {socket_path}"
        ) from exc

    return WorkerSession(socket_path=socket_path, reader=reader, writer=writer)


async def execute_request(
    socket_path: "Path", request: dict[str, object]
) -> dict[str, object]:
    """Executes one OCIApp request against a worker socket."""

    session = await open_worker_session(socket_path)
    response_reader = asyncio.create_task(
        session.read_responses(), name=f"ociapp-runtime-reader:{socket_path.name}"
    )

    try:
        return await session.execute(request)
    finally:
        await session.close()
        with contextlib.suppress(ResponseProtocolError):
            await response_reader
