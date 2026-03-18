from collections.abc import Sequence
from pathlib import Path

import ociapp_build
from ociapp_build.build import (
    _build_image_tag,
    _build_project,
    _prepare_managed_context,
    _resolve_artifact_path,
)
from ociapp_build.config import (
    _BuildProject,
    _load_build_project,
    _ManagedBuildConfig,
    _ProjectMetadata,
)
from ociapp_build.runner import _CommandResult, _CommandRunner


class FakeRunner(_CommandRunner):
    def __init__(self) -> None:
        self.commands: list[tuple[tuple[str, ...], Path | None]] = []

    def run(self, args: Sequence[str], cwd: Path | None = None) -> _CommandResult:
        command = tuple(args)
        self.commands.append((command, cwd))
        if command[:3] == ("uv", "build", "--wheel"):
            out_dir = Path(command[-1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "demo_app-1.2.3-py3-none-any.whl").write_text("wheel")
        if command[:3] == ("docker", "buildx", "build"):
            output_path = _extract_buildx_destination(command)
            output_path.write_text("archive")
        return _CommandResult(args=command, stdout="", stderr="", returncode=0)


EXPECTED_CUSTOM_COMMANDS = 1
EXPECTED_MANAGED_COMMANDS = 2


def write_pyproject(project_root: Path, body: str) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "pyproject.toml").write_text(body.strip() + "\n")


def test_prepare_managed_context_writes_containerfile(tmp_path: Path) -> None:
    build_project_config = _BuildProject(
        root=tmp_path,
        metadata=_ProjectMetadata(name="demo-app", version="1.2.3"),
        config=_ManagedBuildConfig(
            entrypoint="demo.main:app", system_packages=("git",)
        ),
    )
    wheel_path = tmp_path / "demo_app-1.2.3-py3-none-any.whl"
    wheel_path.write_text("wheel")
    config = build_project_config.config
    assert isinstance(config, _ManagedBuildConfig)

    containerfile_path = _prepare_managed_context(
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

    artifact_path = _build_project(project_root, runner=runner)

    assert artifact_path == project_root / "demo-app-1.2.3.ociapp"
    assert artifact_path.exists()
    assert len(runner.commands) == EXPECTED_MANAGED_COMMANDS
    assert runner.commands[0][0][:3] == ("uv", "build", "--wheel")
    assert runner.commands[1][0][:3] == ("docker", "buildx", "build")
    assert _extract_buildx_destination(runner.commands[1][0]) == artifact_path
    assert not any("save" in command for command, _ in runner.commands)


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

    artifact_path = _build_project(project_root, runner=runner)

    assert artifact_path.exists()
    assert len(runner.commands) == EXPECTED_CUSTOM_COMMANDS
    assert runner.commands[0][0][:3] == ("docker", "buildx", "build")
    assert _extract_buildx_destination(runner.commands[0][0]) == artifact_path
    assert not any("save" in command for command, _ in runner.commands)


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
    build_project_config = _load_build_project(project_root)

    assert _build_image_tag(build_project_config) == "ociapp-build/demo-app:1.2.3-abc"
    assert (
        _resolve_artifact_path(build_project_config)
        == project_root / "demo_app-1.2.3+abc.ociapp"
    )


def test_package_root_exports_no_python_api() -> None:
    assert ociapp_build.__all__ == []
    for name in (
        "build_project",
        "build_image_tag",
        "load_build_project",
        "ManagedBuildConfig",
    ):
        assert not hasattr(ociapp_build, name)


def _extract_buildx_destination(command: tuple[str, ...]) -> Path:
    output_spec = command[command.index("--output") + 1]
    for field in output_spec.split(","):
        if field.startswith("dest="):
            return Path(field.removeprefix("dest="))

    raise AssertionError("docker buildx build command did not include a destination")
