import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from .config import CustomBuildConfig, ManagedBuildConfig, load_build_project
from .containerfile import render_managed_containerfile
from .runner import CommandRunner

if TYPE_CHECKING:
    from .config import BuildProject


class BuildArtifactError(Exception):
    """Raised when OCIApp archive construction cannot complete."""


def build_project(
    project_root: Path | str,
    output_dir: Path | str | None = None,
    runner: CommandRunner | None = None,
) -> Path:
    """Builds an OCIApp archive for a target project."""

    build_project_config = load_build_project(project_root)
    command_runner = runner or CommandRunner()
    artifact_path = resolve_artifact_path(build_project_config, output_dir=output_dir)
    image_tag = build_image_tag(build_project_config)

    if isinstance(build_project_config.config, ManagedBuildConfig):
        _build_managed(
            build_project_config,
            artifact_path=artifact_path,
            image_tag=image_tag,
            runner=command_runner,
        )
    else:
        _build_custom(
            build_project_config,
            artifact_path=artifact_path,
            image_tag=image_tag,
            runner=command_runner,
        )

    return artifact_path


def resolve_artifact_path(
    build_project_config: "BuildProject", output_dir: Path | str | None = None
) -> Path:
    """Computes the destination OCIApp archive path."""

    destination_root = (
        build_project_config.root if output_dir is None else Path(output_dir).resolve()
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    return destination_root / build_project_config.metadata.artifact_name


def build_image_tag(build_project_config: "BuildProject") -> str:
    """Builds a stable local Podman tag for a project."""

    normalized_name = build_project_config.metadata.name.replace("_", "-").lower()
    normalized_version = build_project_config.metadata.version.replace("+", "-")
    return f"ociapp-build/{normalized_name}:{normalized_version}"


def build_wheel(
    build_project_config: "BuildProject", wheel_dir: Path, runner: CommandRunner
) -> Path:
    """Builds a wheel for the target project."""

    wheel_dir.mkdir(parents=True, exist_ok=True)
    runner.run(
        ("uv", "build", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=build_project_config.root,
    )
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise BuildArtifactError("managed builds must produce exactly one wheel")

    return wheels[0]


def prepare_managed_context(
    build_project_config: "BuildProject",
    config: ManagedBuildConfig,
    wheel_path: Path,
    context_dir: Path,
) -> Path:
    """Creates the temporary managed build context."""

    dist_dir = context_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    copied_wheel = dist_dir / wheel_path.name
    shutil.copy2(wheel_path, copied_wheel)

    containerfile_path = context_dir / "Containerfile"
    containerfile_path.write_text(
        render_managed_containerfile(config=config, wheel_name=wheel_path.name)
    )
    return containerfile_path


def export_archive(
    artifact_path: Path, image_tag: str, runner: CommandRunner, cwd: Path
) -> None:
    """Exports a built image to an OCI archive."""

    runner.run(
        (
            "podman",
            "save",
            "--format",
            "oci-archive",
            "--output",
            str(artifact_path),
            image_tag,
        ),
        cwd=cwd,
    )


def _build_managed(
    build_project_config: "BuildProject",
    artifact_path: Path,
    image_tag: str,
    runner: CommandRunner,
) -> None:
    config = build_project_config.config
    assert isinstance(config, ManagedBuildConfig)

    with TemporaryDirectory(prefix="ociapp-build-") as temporary_directory:
        temp_root = Path(temporary_directory)
        wheel_path = build_wheel(
            build_project_config, wheel_dir=temp_root / "wheel", runner=runner
        )
        context_dir = temp_root / "context"
        context_dir.mkdir(parents=True, exist_ok=True)
        containerfile_path = prepare_managed_context(
            build_project_config,
            config=config,
            wheel_path=wheel_path,
            context_dir=context_dir,
        )
        runner.run(
            (
                "podman",
                "build",
                "--tag",
                image_tag,
                "--file",
                str(containerfile_path),
                str(context_dir),
            ),
            cwd=build_project_config.root,
        )
        export_archive(
            artifact_path=artifact_path,
            image_tag=image_tag,
            runner=runner,
            cwd=build_project_config.root,
        )


def _build_custom(
    build_project_config: "BuildProject",
    artifact_path: Path,
    image_tag: str,
    runner: CommandRunner,
) -> None:
    config = build_project_config.config
    assert isinstance(config, CustomBuildConfig)
    runner.run(
        (
            "podman",
            "build",
            "--tag",
            image_tag,
            "--file",
            str(config.containerfile),
            str(build_project_config.root),
        ),
        cwd=build_project_config.root,
    )
    export_archive(
        artifact_path=artifact_path,
        image_tag=image_tag,
        runner=runner,
        cwd=build_project_config.root,
    )
