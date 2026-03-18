from typing import TYPE_CHECKING

import pytest
from ociapp_build.config import (
    BuildConfigError,
    CustomBuildConfig,
    ManagedBuildConfig,
    load_build_project,
)

if TYPE_CHECKING:
    from pathlib import Path


def write_pyproject(project_root: "Path", body: str) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "pyproject.toml").write_text(body.strip() + "\n")


@pytest.mark.parametrize(
    ("body", "expected_type"),
    [
        (
            """
[project]
name = "demo-app"
version = "1.2.3"

[tool.ociapp-build]
entrypoint = "demo.main:app"
system-packages = ["git", "curl"]
""",
            ManagedBuildConfig,
        ),
        (
            """
[project]
name = "demo-app"
version = "1.2.3"

[tool.ociapp-build]
mode = "custom"
containerfile = "Containerfile"
""",
            CustomBuildConfig,
        ),
    ],
    ids=["managed", "custom"],
)
def test_load_build_project_valid_configs(
    tmp_path: "Path", body: str, expected_type: type[object]
) -> None:
    project_root = tmp_path / "project"
    write_pyproject(project_root, body)
    (project_root / "Containerfile").write_text("FROM scratch\n")

    build_project_config = load_build_project(project_root)

    assert isinstance(build_project_config.config, expected_type)


@pytest.mark.parametrize(
    "body",
    [
        """
[project]
name = "demo-app"
version = "1.2.3"

[tool.ociapp-build]
""",
        """
[project]
name = "demo-app"
version = "1.2.3"

[tool.ociapp-build]
mode = "custom"
entrypoint = "demo.main:app"
containerfile = "Containerfile"
""",
        """
[project]
name = "demo-app"
version = "1.2.3"

[tool.ociapp-build]
mode = "custom"
containerfile = "Missingfile"
""",
    ],
    ids=[
        "managed-missing-entrypoint",
        "custom-extra-key",
        "custom-missing-containerfile",
    ],
)
def test_load_build_project_invalid_configs(tmp_path: "Path", body: str) -> None:
    project_root = tmp_path / "project"
    write_pyproject(project_root, body)

    with pytest.raises(BuildConfigError):
        load_build_project(project_root)


def test_load_build_project_extracts_metadata(tmp_path: "Path") -> None:
    project_root = tmp_path / "project"
    write_pyproject(
        project_root,
        """
[project]
name = "demo-app"
version = "1.2.3"

[tool.ociapp-build]
entrypoint = "demo.main:app"
""",
    )

    build_project_config = load_build_project(project_root)

    assert build_project_config.metadata.name == "demo-app"
    assert build_project_config.metadata.version == "1.2.3"
    assert build_project_config.metadata.artifact_name == "demo-app-1.2.3.ociapp"
