from pathlib import Path
from typing import TYPE_CHECKING

from ociapp_build.build import (
    build_image_tag,
    build_project,
    prepare_managed_context,
    resolve_artifact_path,
)
from ociapp_build.config import (
    BuildProject,
    ManagedBuildConfig,
    ProjectMetadata,
    load_build_project,
)
from ociapp_build.runner import CommandResult, CommandRunner

if TYPE_CHECKING:
    from collections.abc import Sequence


class FakeRunner(CommandRunner):
    def __init__(self) -> None:
        self.commands: list[tuple[tuple[str, ...], Path | None]] = []

    def run(self, args: "Sequence[str]", cwd: Path | None = None) -> CommandResult:
        command = tuple(args)
        self.commands.append((command, cwd))
        if command[:3] == ("uv", "build", "--wheel"):
            out_dir = Path(command[-1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "demo_app-1.2.3-py3-none-any.whl").write_text("wheel")
        if command[:3] == ("podman", "save", "--format"):
            output_path = Path(command[5])
            output_path.write_text("archive")
        return CommandResult(args=command, stdout="", stderr="", returncode=0)


EXPECTED_CUSTOM_COMMANDS = 2


def write_pyproject(project_root: Path, body: str) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "pyproject.toml").write_text(body.strip() + "\n")


def test_prepare_managed_context_writes_containerfile(tmp_path: Path) -> None:
    build_project_config = BuildProject(
        root=tmp_path,
        metadata=ProjectMetadata(name="demo-app", version="1.2.3"),
        config=ManagedBuildConfig(entrypoint="demo.main:app", system_packages=("git",)),
    )
    wheel_path = tmp_path / "demo_app-1.2.3-py3-none-any.whl"
    wheel_path.write_text("wheel")
    config = build_project_config.config
    assert isinstance(config, ManagedBuildConfig)

    containerfile_path = prepare_managed_context(
        build_project_config,
        config=config,
        wheel_path=wheel_path,
        context_dir=tmp_path / "context",
    )

    assert containerfile_path.exists()
    assert (tmp_path / "context" / "dist" / wheel_path.name).exists()


def test_build_project_managed_constructs_commands(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    write_pyproject(
        project_root,
        """
[project]
name = "demo-app"
version = "1.2.3"

[tool.ociapp-build]
entrypoint = "demo.main:app"
system-packages = ["git"]
""",
    )
    runner = FakeRunner()

    artifact_path = build_project(project_root, runner=runner)

    assert artifact_path == project_root / "demo-app-1.2.3.ociapp"
    assert artifact_path.exists()
    assert runner.commands[0][0][:3] == ("uv", "build", "--wheel")
    assert runner.commands[1][0][:2] == ("podman", "build")
    assert runner.commands[2][0][:2] == ("podman", "save")


def test_build_project_custom_constructs_commands(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    write_pyproject(
        project_root,
        """
[project]
name = "demo-app"
version = "1.2.3"

[tool.ociapp-build]
mode = "custom"
containerfile = "Containerfile"
""",
    )
    (project_root / "Containerfile").write_text("FROM scratch\n")
    runner = FakeRunner()

    artifact_path = build_project(project_root, runner=runner)

    assert artifact_path.exists()
    assert len(runner.commands) == EXPECTED_CUSTOM_COMMANDS
    assert runner.commands[0][0][:2] == ("podman", "build")
    assert runner.commands[1][0][:2] == ("podman", "save")


def test_build_helpers_compute_tags_and_artifacts(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    write_pyproject(
        project_root,
        """
[project]
name = "demo_app"
version = "1.2.3+abc"

[tool.ociapp-build]
entrypoint = "demo.main:app"
""",
    )
    build_project_config = load_build_project(project_root)

    assert build_image_tag(build_project_config) == "ociapp-build/demo-app:1.2.3-abc"
    assert (
        resolve_artifact_path(build_project_config)
        == project_root / "demo_app-1.2.3+abc.ociapp"
    )
