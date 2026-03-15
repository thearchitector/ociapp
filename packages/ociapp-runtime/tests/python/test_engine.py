from typing import TYPE_CHECKING

import pytest
from ociapp_runtime.engine import PodmanAdapter
from ociapp_runtime.errors import (
    ArtifactLoadError,
    InstanceShutdownError,
    InstanceStartupError,
)
from ociapp_runtime.runner import CommandExecutionError, CommandResult, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class FakeRunner(CommandRunner):
    def __init__(self, results: list[CommandResult] | None = None) -> None:
        self.commands: list[tuple[tuple[str, ...], Path | None, float | None]] = []
        self._results = list(results or [])
        self.fail = False

    def run(
        self,
        args: "Sequence[str]",
        cwd: "Path | None" = None,
        timeout: float | None = None,
    ) -> CommandResult:
        command = tuple(args)
        self.commands.append((command, cwd, timeout))
        if self.fail:
            raise CommandExecutionError("boom")
        if self._results:
            return self._results.pop(0)
        return CommandResult(args=command, stdout="", stderr="", returncode=0)


def test_load_archive_parses_loaded_image_output(tmp_path: "Path") -> None:
    artifact_path = tmp_path / "demo.ociapp"
    artifact_path.write_text("archive")
    runner = FakeRunner([
        CommandResult(
            args=("podman", "load"),
            stdout="Loaded image: localhost/demo:1.0.0\n",
            stderr="",
            returncode=0,
        )
    ])
    adapter = PodmanAdapter(runner=runner)

    image_reference = adapter.load_archive(artifact_path)

    assert image_reference == "localhost/demo:1.0.0"
    assert runner.commands[0][0] == ("podman", "load", "--input", str(artifact_path))


def test_load_archive_rejects_missing_artifact(tmp_path: "Path") -> None:
    adapter = PodmanAdapter(runner=FakeRunner())

    with pytest.raises(ArtifactLoadError, match="does not exist"):
        adapter.load_archive(tmp_path / "missing.ociapp")


def test_run_container_constructs_expected_command(tmp_path: "Path") -> None:
    runner = FakeRunner([
        CommandResult(
            args=("podman", "run"), stdout="container-123\n", stderr="", returncode=0
        )
    ])
    adapter = PodmanAdapter(runner=runner)

    container_id = adapter.run_container(
        "localhost/demo:1.0.0", tmp_path, "demo-worker"
    )

    assert container_id == "container-123"
    assert runner.commands[0][0] == (
        "podman",
        "run",
        "--detach",
        "--rm",
        "--name",
        "demo-worker",
        "--mount",
        f"type=bind,src={tmp_path},dst=/run/ociapp",
        "localhost/demo:1.0.0",
    )


def test_run_container_rejects_missing_container_id(tmp_path: "Path") -> None:
    runner = FakeRunner([
        CommandResult(args=("podman", "run"), stdout="", stderr="", returncode=0)
    ])
    adapter = PodmanAdapter(runner=runner)

    with pytest.raises(InstanceStartupError, match="container id"):
        adapter.run_container("localhost/demo:1.0.0", tmp_path, "demo-worker")


def test_stop_container_wraps_command_failures() -> None:
    runner = FakeRunner()
    runner.fail = True
    adapter = PodmanAdapter(runner=runner)

    with pytest.raises(InstanceShutdownError, match="failed to stop container"):
        adapter.stop_container("container-123", 3.0)
