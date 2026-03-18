import re
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol

from .errors import ArtifactLoadError, InstanceShutdownError, InstanceStartupError
from .runner import CommandRunner

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["DockerAdapter", "EngineAdapter"]


class EngineAdapter(Protocol):
    """Defines the engine operations used by the runtime."""

    def load_archive(self, artifact_path: "Path") -> str:
        """Loads an OCI archive and returns an image reference."""

    def run_container(
        self, image_reference: str, mount_dir: "Path", container_name: str
    ) -> str:
        """Starts a worker container and returns its container id."""

    def stop_container(self, container_id: str, timeout_seconds: float) -> None:
        """Stops a running worker container."""

    @staticmethod
    def build_container_name(artifact_path: "Path") -> str:
        """Builds a stable container name prefix for an artifact."""


class DockerAdapter:
    """Wraps Docker command construction for OCIApp runtime workers."""

    def __init__(
        self, runner: CommandRunner | None = None, command_timeout: float = 60.0
    ) -> None:
        self._runner = runner or CommandRunner()
        self._command_timeout = command_timeout

    def load_archive(self, artifact_path: "Path") -> str:
        """Loads an OCI archive and returns the loaded image reference."""

        if not artifact_path.exists():
            raise ArtifactLoadError(f"OCIApp artifact does not exist: {artifact_path}")

        result = self._runner.run(
            ("docker", "load", "--input", str(artifact_path)),
            cwd=artifact_path.parent,
            timeout=self._command_timeout,
        )
        image_reference = _parse_loaded_image_reference(result.stdout, result.stderr)
        if image_reference is None:
            raise ArtifactLoadError(
                "docker load did not report a loaded image reference"
            )

        return image_reference

    def run_container(
        self, image_reference: str, mount_dir: "Path", container_name: str
    ) -> str:
        """Starts a detached OCIApp worker container and returns its id."""

        mount_dir.mkdir(parents=True, exist_ok=True)
        mount_spec = f"type=bind,src={mount_dir},dst=/run/ociapp"
        result = self._runner.run(
            (
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--mount",
                mount_spec,
                image_reference,
            ),
            cwd=mount_dir,
            timeout=self._command_timeout,
        )
        container_id = result.stdout.strip()
        if not container_id:
            raise InstanceStartupError("docker run did not return a container id")

        return container_id

    def stop_container(self, container_id: str, timeout_seconds: float) -> None:
        """Stops a running worker container."""

        stop_timeout = max(1, int(timeout_seconds))
        try:
            self._runner.run(
                ("docker", "stop", "--time", str(stop_timeout), container_id),
                timeout=timeout_seconds + 1,
            )
        except Exception as exc:
            raise InstanceShutdownError(
                f"failed to stop container {container_id}"
            ) from exc

    @staticmethod
    def build_container_name(artifact_path: "Path") -> str:
        """Builds a stable, safe Docker container name prefix."""

        normalized_stem = re.sub(r"[^a-z0-9]+", "-", artifact_path.stem.lower()).strip(
            "-"
        )
        digest = sha256(str(artifact_path).encode()).hexdigest()[:12]
        prefix = normalized_stem or "ociapp"
        return f"ociapp-{prefix}-{digest}"


def _parse_loaded_image_reference(stdout: str, stderr: str) -> str | None:
    for line in (stdout + "\n" + stderr).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in {"Loaded image", "Loaded image(s)"}:
            image_reference = value.strip()
            if image_reference:
                return image_reference

    return None
