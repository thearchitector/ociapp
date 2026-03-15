from dataclasses import dataclass
from pathlib import Path

import tomllib


class BuildConfigError(Exception):
    """Raised when OCIApp build configuration is invalid."""


@dataclass(slots=True, frozen=True)
class ProjectMetadata:
    """Describes the canonical Python project metadata."""

    name: str
    version: str

    @property
    def artifact_name(self) -> str:
        """Returns the OCIApp archive filename."""

        return f"{self.name}-{self.version}.ociapp"


@dataclass(slots=True, frozen=True)
class ManagedBuildConfig:
    """Represents the default OCIApp managed build mode."""

    entrypoint: str
    system_packages: tuple[str, ...]
    mode: str = "managed"


@dataclass(slots=True, frozen=True)
class CustomBuildConfig:
    """Represents the OCIApp custom Containerfile mode."""

    containerfile: Path
    mode: str = "custom"


type BuildConfig = ManagedBuildConfig | CustomBuildConfig


@dataclass(slots=True, frozen=True)
class BuildProject:
    """Represents a target project and its OCIApp build configuration."""

    root: Path
    metadata: ProjectMetadata
    config: BuildConfig


def load_build_project(project_root: Path | str) -> BuildProject:
    """Loads OCIApp build configuration from a target project."""

    root = Path(project_root).resolve()
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.exists():
        raise BuildConfigError(f"missing pyproject.toml at {pyproject_path}")

    raw = tomllib.loads(pyproject_path.read_text())
    metadata = _load_metadata(raw)
    config = _load_config(raw, project_root=root)
    return BuildProject(root=root, metadata=metadata, config=config)


def _load_metadata(raw: dict[str, object]) -> ProjectMetadata:
    project_data = _require_table(raw.get("project"), "project")
    name = _require_string(project_data.get("name"), "project.name")
    version = _require_string(project_data.get("version"), "project.version")
    return ProjectMetadata(name=name, version=version)


def _load_config(raw: dict[str, object], project_root: Path) -> BuildConfig:
    tool_data = _require_table(raw.get("tool"), "tool")
    build_data = _require_table(tool_data.get("ociapp-build"), "tool.ociapp-build")
    mode = build_data.get("mode", "managed")
    if mode == "managed":
        return _load_managed_config(build_data)
    if mode == "custom":
        return _load_custom_config(build_data, project_root=project_root)

    raise BuildConfigError("tool.ociapp-build.mode must be 'managed' or 'custom'")


def _load_managed_config(build_data: dict[str, object]) -> ManagedBuildConfig:
    _reject_unknown_keys(
        build_data, allowed_keys={"mode", "entrypoint", "system-packages"}
    )
    entrypoint = _require_string(
        build_data.get("entrypoint"), "tool.ociapp-build.entrypoint"
    )
    system_packages = _load_system_packages(build_data.get("system-packages"))
    return ManagedBuildConfig(entrypoint=entrypoint, system_packages=system_packages)


def _load_custom_config(
    build_data: dict[str, object], project_root: Path
) -> CustomBuildConfig:
    _reject_unknown_keys(build_data, allowed_keys={"mode", "containerfile"})
    containerfile_value = _require_string(
        build_data.get("containerfile"), "tool.ociapp-build.containerfile"
    )
    containerfile = (project_root / containerfile_value).resolve()
    if not containerfile.exists():
        raise BuildConfigError(
            f"custom Containerfile does not exist: {containerfile.relative_to(project_root)}"
        )

    return CustomBuildConfig(containerfile=containerfile)


def _load_system_packages(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BuildConfigError(
            "tool.ociapp-build.system-packages must be an array of strings"
        )

    return tuple(value)


def _reject_unknown_keys(data: dict[str, object], allowed_keys: set[str]) -> None:
    unknown_keys = sorted(set(data).difference(allowed_keys))
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise BuildConfigError(f"unsupported OCIApp build keys: {joined}")


def _require_table(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BuildConfigError(f"missing [{field_name}] configuration")

    if not all(isinstance(key, str) for key in value):
        raise BuildConfigError(f"[{field_name}] keys must be strings")

    return value


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildConfigError(f"{field_name} must be a non-empty string")

    return value
