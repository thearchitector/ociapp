from pathlib import Path

from ociapp_build.config import ManagedBuildConfig, load_build_project

EXAMPLES_ROOT = Path(__file__).resolve().parents[4] / "examples"
README_PATH = Path(__file__).resolve().parents[4] / "README.md"


def test_echo_app_example_matches_root_readme_contract() -> None:
    build_project_config = load_build_project(EXAMPLES_ROOT / "echo-app")

    assert isinstance(build_project_config.config, ManagedBuildConfig)
    assert build_project_config.config.entrypoint == "echo_app.main:app"
    assert build_project_config.metadata.artifact_name == "echo-app-0.1.0.ociapp"


def test_root_readme_includes_request_flow_diagram() -> None:
    readme = README_PATH.read_text()

    assert "## Request Flow" in readme
    assert "```mermaid" in readme
    assert "participant RT as Runtime" in readme
    assert "participant C as Container" in readme
    assert "RT->>C: send request over UDS" in readme
    assert "S->>App: validate + execute" in readme
    assert "RT-->>Host: return result" in readme
