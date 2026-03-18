from pathlib import Path

import pytest
from ociapp_build.cli import main


def test_cli_invokes_builder_with_output_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_build(project_root: str, output_dir: str | None = None) -> Path:
        captured["project_root"] = project_root
        captured["output_dir"] = output_dir
        return Path("dist/demo.ociapp")

    monkeypatch.setattr("ociapp_build.cli._build_project", fake_build)

    result = main(["example/echo-app", "--output-dir", "dist"])

    assert result == 0
    assert captured == {"project_root": "example/echo-app", "output_dir": "dist"}
