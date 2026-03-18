import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from ociapp.errors import ErrorPayload

import ociapp_runtime.runtime as runtime_module
from ociapp_runtime import DockerAdapter, Runtime
from ociapp_runtime.errors import (
    InstanceStartupError,
    OCIAppRuntimeError,
    RemoteExecutionError,
    RequestTimeoutError,
    ResponseProtocolError,
)

REPLACEMENT_WORKER_COUNT = 2


class FakeEngine:
    def __init__(self, *, create_socket: bool = True) -> None:
        self.create_socket = create_socket
        self.load_calls: list[Path] = []
        self.run_calls: list[tuple[str, Path, str]] = []
        self.stop_calls: list[tuple[str, float]] = []

    def load_archive(self, artifact_path: Path) -> str:
        self.load_calls.append(artifact_path)
        return f"loaded/{artifact_path.stem}:1.0"

    def run_container(
        self, image_reference: str, mount_dir: Path, container_name: str
    ) -> str:
        self.run_calls.append((image_reference, mount_dir, container_name))
        mount_dir.mkdir(parents=True, exist_ok=True)
        if self.create_socket:
            (mount_dir / "app.sock").touch()
        return f"{container_name}-cid"

    def stop_container(self, container_id: str, timeout_seconds: float) -> None:
        self.stop_calls.append((container_id, timeout_seconds))

    @staticmethod
    def build_container_name(artifact_path: Path) -> str:
        return f"worker-{artifact_path.stem}"


class EchoSession:
    def __init__(self) -> None:
        self.is_open = True
        self.close_errors: list[BaseException | None] = []
        self.execute_calls: list[tuple[dict[str, object], float | None]] = []
        self._closed = asyncio.Event()

    async def execute(
        self, request: dict[str, object], *, request_timeout: float | None = None
    ) -> dict[str, object]:
        self.execute_calls.append((request, request_timeout))
        return dict(request)

    async def read_responses(self) -> None:
        await self._closed.wait()

    async def close(self, error: BaseException | None = None) -> None:
        self.is_open = False
        self.close_errors.append(error)
        self._closed.set()


class ControlledSession(EchoSession):
    def __init__(self) -> None:
        super().__init__()
        self.pending: dict[str, asyncio.Future[dict[str, object]]] = {}
        self._pending_condition = asyncio.Condition()
        self._read_error: ResponseProtocolError | None = None

    async def execute(
        self, request: dict[str, object], *, request_timeout: float | None = None
    ) -> dict[str, object]:
        self.execute_calls.append((request, request_timeout))
        key = str(request["value"])
        response_future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[key] = response_future
        async with self._pending_condition:
            self._pending_condition.notify_all()

        try:
            if request_timeout is None:
                return await response_future

            try:
                async with asyncio.timeout(request_timeout):
                    return await asyncio.shield(response_future)
            except TimeoutError as exc:
                pending_future = self.pending.pop(key, None)
                if pending_future is response_future:
                    response_future.cancel()
                    raise RequestTimeoutError(
                        f"request timed out after {request_timeout}s"
                    ) from exc
                return await response_future
        except asyncio.CancelledError:
            pending_future = self.pending.pop(key, None)
            if pending_future is response_future:
                response_future.cancel()
            raise
        except BaseException:
            pending_future = self.pending.pop(key, None)
            if pending_future is response_future and not response_future.done():
                response_future.cancel()
            raise

    async def read_responses(self) -> None:
        await self._closed.wait()
        if self._read_error is not None:
            self._fail_pending(self._read_error)
            raise self._read_error

    async def close(self, error: BaseException | None = None) -> None:
        if error is not None:
            self._fail_pending(error)
        await super().close(error)

    async def wait_for_pending(self, count: int) -> None:
        async with self._pending_condition:
            await self._pending_condition.wait_for(lambda: len(self.pending) >= count)

    def resolve(self, key: str, value: dict[str, object]) -> None:
        pending_future = self.pending.pop(key)
        pending_future.set_result(value)

    def fail(self, key: str, error: BaseException) -> None:
        pending_future = self.pending.pop(key)
        pending_future.set_exception(error)

    def fail_transport(self, message: str) -> None:
        self._read_error = ResponseProtocolError(message)
        self._closed.set()

    def _fail_pending(self, error: BaseException) -> None:
        pending = list(self.pending.values())
        self.pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)


class SessionFactory:
    def __init__(self, sessions: list[EchoSession]) -> None:
        self._sessions = sessions
        self.opened_paths: list[Path] = []

    async def open(self, socket_path: Path) -> EchoSession:
        self.opened_paths.append(socket_path)
        if not self._sessions:
            raise AssertionError("no fake sessions were left to open")
        return self._sessions.pop(0)


def test_runtime_defaults_to_docker_adapter() -> None:
    runtime = Runtime()

    assert isinstance(runtime._engine, DockerAdapter)


@pytest.mark.asyncio
async def test_runtime_uses_managed_temporary_root_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup_calls: list[Path] = []

    class FakeTemporaryDirectory:
        def __init__(self, *, prefix: str) -> None:
            self.name = str(tmp_path / f"{prefix}managed")
            Path(self.name).mkdir()

        def cleanup(self) -> None:
            path = Path(self.name)
            cleanup_calls.append(path)
            path.rmdir()

    monkeypatch.setattr(runtime_module, "TemporaryDirectory", FakeTemporaryDirectory)

    runtime = Runtime()

    assert runtime._runtime_root is None

    await runtime.__aenter__()

    managed_root = (tmp_path / "ociapp-runtime-managed").resolve()
    assert runtime._runtime_root == managed_root

    await runtime.__aexit__(None, None, None)

    assert cleanup_calls == [managed_root]
    assert runtime._runtime_root is None
    assert not managed_root.exists()


@pytest.mark.asyncio
async def test_runtime_requires_lifecycle_start(tmp_path: Path) -> None:
    runtime = Runtime(engine=FakeEngine())

    with pytest.raises(OCIAppRuntimeError):
        await runtime.execute(tmp_path / "demo.ociapp", {"value": "payload"})


@pytest.mark.asyncio
async def test_runtime_reuses_warm_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    session = EchoSession()
    factory = SessionFactory([session])
    artifact = create_artifact(tmp_path)

    monkeypatch.setattr(runtime_module, "_open_worker_session", factory.open)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine, idle_timeout=10.0) as runtime:
        first = await runtime.execute(artifact, {"value": "hello"})
        second = await runtime.execute(artifact, {"value": "world"})

    assert first == {"value": "hello"}
    assert second == {"value": "world"}
    assert engine.load_calls == [artifact.resolve()]
    assert len(engine.run_calls) == 1
    assert len(factory.opened_paths) == 1


@pytest.mark.asyncio
async def test_runtime_reaps_idle_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    session = EchoSession()
    factory = SessionFactory([session])
    artifact = create_artifact(tmp_path)

    monkeypatch.setattr(runtime_module, "_open_worker_session", factory.open)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(
        engine=engine, idle_timeout=0.01, reaper_interval=0.01
    ) as runtime:
        await runtime.execute(artifact, {"value": "hello"})
        await wait_for(lambda: len(engine.stop_calls) == 1)

    assert len(engine.stop_calls) == 1


@pytest.mark.asyncio
async def test_runtime_enforces_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine(create_socket=False)
    artifact = create_artifact(tmp_path)

    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine, startup_timeout=0.05) as runtime:
        with pytest.raises(InstanceStartupError):
            await runtime.execute(artifact, {"value": "hello"})


@pytest.mark.asyncio
async def test_runtime_keeps_worker_after_per_request_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    session = ControlledSession()
    factory = SessionFactory([session])
    artifact = create_artifact(tmp_path)

    monkeypatch.setattr(runtime_module, "_open_worker_session", factory.open)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine, request_timeout=0.05) as runtime:
        timed_out_task = asyncio.create_task(
            runtime.execute(artifact, {"value": "slow"})
        )
        await session.wait_for_pending(1)

        with pytest.raises(RequestTimeoutError):
            await timed_out_task

        second_task = asyncio.create_task(runtime.execute(artifact, {"value": "next"}))
        await session.wait_for_pending(1)
        session.resolve("next", {"value": "next"})

        assert await second_task == {"value": "next"}

    assert len(engine.run_calls) == 1
    assert engine.stop_calls == [(f"{engine.run_calls[0][2]}-cid", 10.0)]


@pytest.mark.asyncio
async def test_runtime_multiplexes_concurrent_requests_on_one_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    session = ControlledSession()
    factory = SessionFactory([session])
    artifact = create_artifact(tmp_path)

    monkeypatch.setattr(runtime_module, "_open_worker_session", factory.open)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine) as runtime:
        first_task = asyncio.create_task(runtime.execute(artifact, {"value": "first"}))
        second_task = asyncio.create_task(
            runtime.execute(artifact, {"value": "second"})
        )
        await session.wait_for_pending(2)

        session.resolve("second", {"value": "second"})
        session.resolve("first", {"value": "first"})

        assert await second_task == {"value": "second"}
        assert await first_task == {"value": "first"}

    assert len(engine.run_calls) == 1


@pytest.mark.asyncio
async def test_runtime_coalesces_concurrent_cold_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    session = ControlledSession()
    artifact = create_artifact(tmp_path)
    open_started = asyncio.Event()
    open_release = asyncio.Event()

    async def fake_open_worker_session(socket_path: Path) -> ControlledSession:
        open_started.set()
        await open_release.wait()
        return session

    monkeypatch.setattr(
        runtime_module, "_open_worker_session", fake_open_worker_session
    )
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine) as runtime:
        first_task = asyncio.create_task(runtime.execute(artifact, {"value": "first"}))
        await open_started.wait()
        second_task = asyncio.create_task(
            runtime.execute(artifact, {"value": "second"})
        )

        await asyncio.sleep(0)
        assert len(engine.run_calls) == 1

        open_release.set()
        await session.wait_for_pending(2)
        session.resolve("second", {"value": "second"})
        session.resolve("first", {"value": "first"})

        assert await second_task == {"value": "second"}
        assert await first_task == {"value": "first"}

    assert len(engine.run_calls) == 1


@pytest.mark.asyncio
async def test_runtime_propagates_remote_errors_without_retiring_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    session = ControlledSession()
    factory = SessionFactory([session])
    artifact = create_artifact(tmp_path)

    monkeypatch.setattr(runtime_module, "_open_worker_session", factory.open)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine) as runtime:
        failing_task = asyncio.create_task(runtime.execute(artifact, {"value": "boom"}))
        await session.wait_for_pending(1)
        session.fail(
            "boom",
            RemoteExecutionError(
                ErrorPayload(
                    error_type="Boom", message="failed", details={"reason": "test"}
                )
            ),
        )

        with pytest.raises(RemoteExecutionError) as exc_info:
            await failing_task

        succeeding_task = asyncio.create_task(
            runtime.execute(artifact, {"value": "ok"})
        )
        await session.wait_for_pending(1)
        session.resolve("ok", {"value": "ok"})

        assert exc_info.value.error.error_type == "Boom"
        assert await succeeding_task == {"value": "ok"}

    assert len(engine.run_calls) == 1


@pytest.mark.asyncio
async def test_runtime_retires_failed_transport_and_replaces_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    first_session = ControlledSession()
    second_session = EchoSession()
    factory = SessionFactory([first_session, second_session])
    artifact = create_artifact(tmp_path)

    monkeypatch.setattr(runtime_module, "_open_worker_session", factory.open)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine) as runtime:
        first_task = asyncio.create_task(runtime.execute(artifact, {"value": "first"}))
        second_task = asyncio.create_task(
            runtime.execute(artifact, {"value": "second"})
        )
        await first_session.wait_for_pending(2)

        first_session.fail_transport("worker closed the connection before responding")

        with pytest.raises(ResponseProtocolError):
            await first_task
        with pytest.raises(ResponseProtocolError):
            await second_task

        await wait_for(lambda: len(engine.stop_calls) == 1)
        assert await runtime.execute(artifact, {"value": "replacement"}) == {
            "value": "replacement"
        }

    assert len(engine.run_calls) == REPLACEMENT_WORKER_COUNT


@pytest.mark.asyncio
async def test_runtime_close_fails_in_flight_requests_and_stops_container_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    session = ControlledSession()
    factory = SessionFactory([session])
    artifact = create_artifact(tmp_path)

    monkeypatch.setattr(runtime_module, "_open_worker_session", factory.open)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    runtime = Runtime(engine=engine)
    await runtime.__aenter__()

    request_task = asyncio.create_task(runtime.execute(artifact, {"value": "slow"}))
    await session.wait_for_pending(1)
    await runtime.__aexit__(None, None, None)

    with pytest.raises(OCIAppRuntimeError, match="runtime is closing"):
        await request_task

    assert len(engine.stop_calls) == 1


def create_artifact(tmp_path: Path) -> Path:
    artifact = tmp_path / "demo.ociapp"
    artifact.write_bytes(b"archive")
    return artifact


async def wait_for(
    predicate: Callable[[], bool], max_wait: float = 0.5, interval: float = 0.01
) -> None:
    deadline = asyncio.get_running_loop().time() + max_wait
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)

    raise AssertionError("condition was not met before the timeout")
