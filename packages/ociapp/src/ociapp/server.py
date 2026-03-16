import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ValidationError

from .errors import ErrorPayload, PayloadCodecError, ProtocolError, ServerLifecycleError
from .models import ResponseEnvelope
from .payloads import decode_payload, encode_payload
from .protocol import (
    DEFAULT_SOCKET_PATH,
    decode_request_envelope,
    encode_error_payload,
    encode_response_envelope,
    read_frame,
    write_frame,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .application import Application
    from .models import RequestEnvelope


class OciAppServer[RequestT: BaseModel, ResponseT: BaseModel]:
    """Serves an OCIApp application over a Unix domain socket."""

    def __init__(
        self,
        app: "Application[RequestT, ResponseT]",
        socket_path: Path | str = DEFAULT_SOCKET_PATH,
    ) -> None:
        self._app = app
        self._socket_path = Path(socket_path)
        self._server: asyncio.Server | None = None

    @property
    def socket_path(self) -> Path:
        """Returns the configured socket path."""

        return self._socket_path

    async def __aenter__(self) -> "OciAppServer[RequestT, ResponseT]":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        """Starts the underlying Unix domain socket server."""

        await self._prepare_socket_path()
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self._socket_path)
        )

    async def serve_forever(self) -> None:
        """Starts the server and blocks until it is cancelled."""

        async with self:
            server = self._server
            if server is None:
                raise ServerLifecycleError("server did not start successfully")
            await server.serve_forever()

    async def close(self) -> None:
        """Stops the server and removes its socket file."""

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

    async def _prepare_socket_path(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists() or self._socket_path.is_socket():
            self._socket_path.unlink()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    frame = await read_frame(reader)
                except ProtocolError:
                    break
                if frame is None:
                    break

                try:
                    response_payload = await self._handle_request(frame)
                except ProtocolError:
                    break

                await write_frame(writer, response_payload)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _handle_request(self, frame_payload: bytes) -> bytes:
        envelope = decode_request_envelope(frame_payload)
        try:
            execute = cast(
                "Callable[[dict[str, object]], Awaitable[ResponseT]]", self._app.execute
            )
            response_model = await execute(decode_payload(envelope.payload))
            response_payload = encode_payload(response_model.model_dump(mode="python"))
            return encode_response_envelope(
                ResponseEnvelope(
                    request_id=envelope.request_id, payload=response_payload, error=None
                )
            )
        except ValidationError as exc:
            return self._encode_error_response(
                envelope,
                ErrorPayload(
                    error_type="ValidationError",
                    message="application payload validation failed",
                    details={"errors": exc.errors()},
                ),
            )
        except PayloadCodecError as exc:
            return self._encode_error_response(
                envelope,
                ErrorPayload(
                    error_type="PayloadCodecError", message=str(exc), details=None
                ),
            )
        except Exception as exc:
            return self._encode_error_response(
                envelope,
                ErrorPayload(
                    error_type=type(exc).__name__,
                    message=str(exc) or type(exc).__name__,
                    details=None,
                ),
            )

    def _encode_error_response(
        self, envelope: "RequestEnvelope", error: ErrorPayload
    ) -> bytes:
        return encode_response_envelope(
            ResponseEnvelope(
                request_id=envelope.request_id,
                payload=None,
                error=encode_error_payload(error),
            )
        )


async def serve_application(
    app: "Application[BaseModel, BaseModel]",
    socket_path: Path | str = DEFAULT_SOCKET_PATH,
) -> None:
    """Serves an application until cancelled."""

    server = OciAppServer(app=app, socket_path=socket_path)
    await server.serve_forever()
