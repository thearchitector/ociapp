from collections.abc import Sequence
from pathlib import Path

import pytest

import ociapp_runtime
from ociapp_runtime.engine import DockerAdapter
from ociapp_runtime.errors import (
    ArtifactLoadError,
    InstanceShutdownError,
    InstanceStartupError,
)
from ociapp_runtime.runner import _CommandExecutionError, _CommandResult, _CommandRunner


class FakeRunner(_CommandRunner):
    def __init__(self, results: list[_CommandResult] | None = None) -> None:
        self.commands: list[tuple[tuple[str, ...], Path | None, float | None]] = []
        self._results = list(results or [])
        self.fail = False

    def run(
        self, args: Sequence[str], cwd: Path | None = None, timeout: float | None = None
    ) -> _CommandResult:
        command = tuple(args)
        self.commands.append((command, cwd, timeout))
        if self.fail:
            raise _CommandExecutionError("boom")
        if self._results:
            return self._results.pop(0)
        return _CommandResult(args=command, stdout="", stderr="", returncode=0)


def make_adapter(runner: FakeRunner) -> DockerAdapter:
    adapter = DockerAdapter()
    adapter._runner = runner
    return adapter


def test_load_archive_parses_loaded_image_output(tmp_path: Path) -> None:
    artifact_path = tmp_path / "demo.ociapp"
    artifact_path.write_text("archive")
    runner = FakeRunner([
        _CommandResult(
            args=("docker", "load"),
            stdout="Loaded image: localhost/demo:1.0.0\n",
            stderr="",
            returncode=0,
        )
    ])
    adapter = make_adapter(runner)

    image_reference = adapter.load_archive(artifact_path)

    assert image_reference == "localhost/demo:1.0.0"
    assert runner.commands[0][0] == ("docker", "load", "--input", str(artifact_path))


def test_load_archive_rejects_missing_loaded_image_reference(tmp_path: Path) -> None:
    artifact_path = tmp_path / "demo.ociapp"
    artifact_path.write_text("archive")
    runner = FakeRunner([
        _CommandResult(
            args=("docker", "load"),
            stdout="Loaded image ID: sha256:123\n",
            stderr="",
            returncode=0,
        )
    ])
    adapter = make_adapter(runner)

    with pytest.raises(ArtifactLoadError, match="docker load did not report"):
        adapter.load_archive(artifact_path)


def test_load_archive_rejects_missing_artifact(tmp_path: Path) -> None:
    adapter = make_adapter(FakeRunner())

    with pytest.raises(ArtifactLoadError, match="does not exist"):
        adapter.load_archive(tmp_path / "missing.ociapp")


def test_run_container_constructs_expected_command(tmp_path: Path) -> None:
    runner = FakeRunner([
        _CommandResult(
            args=("docker", "run"), stdout="container-123\n", stderr="", returncode=0
        )
    ])
    adapter = make_adapter(runner)

    container_id = adapter.run_container(
        "localhost/demo:1.0.0", tmp_path, "demo-worker"
    )

    assert container_id == "container-123"
    assert tmp_path.stat().st_mode & 0o777 == 0o733
    assert runner.commands[0][0] == (
        "docker",
        "run",
        "--detach",
        "--rm",
        "--name",
        "demo-worker",
        "--mount",
        f"type=bind,src={tmp_path},dst=/run/ociapp",
        "localhost/demo:1.0.0",
    )


def test_run_container_rejects_missing_container_id(tmp_path: Path) -> None:
    runner = FakeRunner([
        _CommandResult(args=("docker", "run"), stdout="", stderr="", returncode=0)
    ])
    adapter = make_adapter(runner)

    with pytest.raises(InstanceStartupError, match="container id"):
        adapter.run_container("localhost/demo:1.0.0", tmp_path, "demo-worker")


def test_stop_container_wraps_command_failures() -> None:
    runner = FakeRunner()
    runner.fail = True
    adapter = make_adapter(runner)

    with pytest.raises(InstanceShutdownError, match="failed to stop container"):
        adapter.stop_container("container-123", 3.0)

    assert runner.commands[0][0] == (
        "docker",
        "stop",
        "--timeout",
        "3",
        "container-123",
    )


def test_runtime_package_exports_supported_surface_only() -> None:
    assert ociapp_runtime.__all__ == ["DockerAdapter", "Runtime"]
    assert hasattr(ociapp_runtime, "DockerAdapter")
    assert hasattr(ociapp_runtime, "Runtime")
    for name in ("EngineAdapter", "RequestTimeoutError", "InstanceState"):
        assert not hasattr(ociapp_runtime, name)
