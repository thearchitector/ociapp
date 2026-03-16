import asyncio
from typing import TYPE_CHECKING

import ociapp_runtime.runtime as runtime_module
import pytest
from ociapp import ErrorPayload
from ociapp_runtime import DockerAdapter
from ociapp_runtime.errors import (
    InstanceStartupError,
    OCIAppRuntimeError,
    RemoteExecutionError,
    RequestTimeoutError,
    ResponseProtocolError,
)
from ociapp_runtime.runtime import Runtime

if TYPE_CHECKING:
    from pathlib import Path


class FakeEngine:
    def __init__(self, *, create_socket: bool = True) -> None:
        self.create_socket = create_socket
        self.load_calls: list[Path] = []
        self.run_calls: list[tuple[str, Path, str]] = []
        self.stop_calls: list[tuple[str, float]] = []

    def load_archive(self, artifact_path: "Path") -> str:
        self.load_calls.append(artifact_path)
        return f"loaded/{artifact_path.stem}:1.0"

    def run_container(
        self, image_reference: str, mount_dir: "Path", container_name: str
    ) -> str:
        self.run_calls.append((image_reference, mount_dir, container_name))
        mount_dir.mkdir(parents=True, exist_ok=True)
        if self.create_socket:
            (mount_dir / "app.sock").touch()
        return f"{container_name}-cid"

    def stop_container(self, container_id: str, timeout_seconds: float) -> None:
        self.stop_calls.append((container_id, timeout_seconds))

    @staticmethod
    def build_container_name(artifact_path: "Path") -> str:
        return f"worker-{artifact_path.stem}"


def test_runtime_defaults_to_docker_adapter(tmp_path: "Path") -> None:
    runtime = Runtime(runtime_root=tmp_path)

    assert isinstance(runtime._engine, DockerAdapter)


@pytest.mark.asyncio
async def test_runtime_requires_lifecycle_start(tmp_path: "Path") -> None:
    runtime = Runtime(engine=FakeEngine(), runtime_root=tmp_path)

    with pytest.raises(OCIAppRuntimeError):
        await runtime.execute(tmp_path / "demo.ociapp", {"value": "payload"})


@pytest.mark.asyncio
async def test_runtime_reuses_warm_instances(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    artifact = tmp_path / "demo.ociapp"
    artifact.write_bytes(b"archive")

    async def fake_execute_request(
        socket_path: "Path", request: dict[str, object]
    ) -> dict[str, object]:
        assert socket_path.name == "app.sock"
        return request

    monkeypatch.setattr(runtime_module, "execute_request", fake_execute_request)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(
        engine=engine, runtime_root=tmp_path / "runtime", idle_timeout=10.0
    ) as runtime:
        first = await runtime.execute(artifact, {"value": "hello"})
        second = await runtime.execute(artifact, {"value": "world"})

    assert first == {"value": "hello"}
    assert second == {"value": "world"}
    assert engine.load_calls == [artifact.resolve()]
    assert len(engine.run_calls) == 1


@pytest.mark.asyncio
async def test_runtime_reaps_idle_instances(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    artifact = tmp_path / "demo.ociapp"
    artifact.write_bytes(b"archive")

    async def fake_execute_request(
        socket_path: "Path", request: dict[str, object]
    ) -> dict[str, object]:
        return request

    monkeypatch.setattr(runtime_module, "execute_request", fake_execute_request)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(
        engine=engine,
        runtime_root=tmp_path / "runtime",
        idle_timeout=0.01,
        reaper_interval=0.01,
    ) as runtime:
        await runtime.execute(artifact, {"value": "hello"})
        await asyncio.sleep(0.1)

    assert len(engine.stop_calls) >= 1


@pytest.mark.asyncio
async def test_runtime_enforces_startup_timeout(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine(create_socket=False)
    artifact = tmp_path / "demo.ociapp"
    artifact.write_bytes(b"archive")

    async def fake_execute_request(
        socket_path: "Path", request: dict[str, object]
    ) -> dict[str, object]:
        return request

    monkeypatch.setattr(runtime_module, "execute_request", fake_execute_request)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(
        engine=engine, runtime_root=tmp_path / "runtime", startup_timeout=0.05
    ) as runtime:
        with pytest.raises(InstanceStartupError):
            await runtime.execute(artifact, {"value": "hello"})


@pytest.mark.asyncio
async def test_runtime_enforces_request_timeout(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    artifact = tmp_path / "demo.ociapp"
    artifact.write_bytes(b"archive")

    async def fake_execute_request(
        socket_path: "Path", request: dict[str, object]
    ) -> dict[str, object]:
        await asyncio.sleep(0.2)
        return request

    monkeypatch.setattr(runtime_module, "execute_request", fake_execute_request)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(
        engine=engine, runtime_root=tmp_path / "runtime", request_timeout=0.05
    ) as runtime:
        with pytest.raises(RequestTimeoutError):
            await runtime.execute(artifact, {"value": "hello"})


@pytest.mark.asyncio
async def test_runtime_retires_instances_on_protocol_errors(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    artifact = tmp_path / "demo.ociapp"
    artifact.write_bytes(b"archive")

    async def fake_execute_request(
        socket_path: "Path", request: dict[str, object]
    ) -> dict[str, object]:
        raise ResponseProtocolError("mismatched request id")

    monkeypatch.setattr(runtime_module, "execute_request", fake_execute_request)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine, runtime_root=tmp_path / "runtime") as runtime:
        with pytest.raises(ResponseProtocolError):
            await runtime.execute(artifact, {"value": "payload"})

    assert len(engine.stop_calls) == 1


@pytest.mark.asyncio
async def test_runtime_propagates_remote_errors(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    artifact = tmp_path / "demo.ociapp"
    artifact.write_bytes(b"archive")

    async def fake_execute_request(
        socket_path: "Path", request: dict[str, object]
    ) -> dict[str, object]:
        raise RemoteExecutionError(
            ErrorPayload(
                error_type="Boom", message="failed", details={"reason": "test"}
            )
        )

    monkeypatch.setattr(runtime_module, "execute_request", fake_execute_request)
    monkeypatch.setattr(runtime_module, "_is_socket", lambda path: path.exists())

    async with Runtime(engine=engine, runtime_root=tmp_path / "runtime") as runtime:
        with pytest.raises(RemoteExecutionError) as exc_info:
            await runtime.execute(artifact, {"value": "payload"})

    assert exc_info.value.error.error_type == "Boom"
