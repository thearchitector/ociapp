from typing import TYPE_CHECKING

import pytest
from ociapp import Application, load_application
from ociapp.cli import main
from ociapp.errors import ApplicationLoadError

if TYPE_CHECKING:
    from pathlib import Path


def test_load_application_returns_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Path"
) -> None:
    module_path = _write_module(tmp_path)
    monkeypatch.syspath_prepend(str(module_path.parent))

    app = load_application("sample_app:app")

    assert isinstance(app, Application)
    assert app.request_model.__name__ == "SampleRequest"
    assert app.response_model.__name__ == "SampleResponse"


@pytest.mark.parametrize(
    ("import_path", "pattern"),
    [
        ("sample_app", "must have the form"),
        ("missing_module:app", "could not import module"),
        ("sample_app:missing", "could not find attribute"),
        ("sample_app:not_app", "must resolve to an Application"),
        ("sample_app:broken_app", "must annotate its response"),
    ],
    ids=[
        "bad-format",
        "missing-module",
        "missing-attribute",
        "wrong-object",
        "broken-app",
    ],
)
def test_load_application_rejects_invalid_import_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Path", import_path: str, pattern: str
) -> None:
    module_path = _write_module(tmp_path)
    monkeypatch.syspath_prepend(str(module_path.parent))

    with pytest.raises(ApplicationLoadError, match=pattern):
        load_application(import_path)


def test_cli_loads_app_and_passes_socket_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: "Path"
) -> None:
    module_path = _write_module(tmp_path)
    monkeypatch.syspath_prepend(str(module_path.parent))
    captured: dict[str, object] = {}

    async def fake_serve(app: object, *, socket_path: "Path") -> None:
        captured["app"] = app
        captured["socket_path"] = socket_path

    monkeypatch.setattr("ociapp.cli.serve_application_spec", fake_serve)
    socket_path = tmp_path / "nested" / "app.sock"

    exit_code = main([
        "serve",
        "--app",
        "sample_app:app",
        "--socket-path",
        str(socket_path),
    ])

    assert exit_code == 0
    assert isinstance(captured["app"], Application)
    assert captured["socket_path"] == socket_path


def _write_module(tmp_path: "Path") -> "Path":
    module_path = tmp_path / "sample_app.py"
    module_path.write_text(
        "\n".join([
            "from pydantic import BaseModel",
            "from ociapp import Application",
            "",
            "class SampleRequest(BaseModel):",
            "    value: int",
            "",
            "class SampleResponse(BaseModel):",
            "    value: int",
            "",
            "class SampleApplication(Application[SampleRequest, SampleResponse]):",
            "    async def execute(self, request: SampleRequest) -> SampleResponse:",
            "        return SampleResponse(value=request.value)",
            "",
            "class BrokenApplication(Application[SampleRequest, SampleResponse]):",
            "    async def execute(self, request: SampleRequest):",
            "        return SampleResponse(value=request.value)",
            "",
            "app = SampleApplication()",
            "broken_app = BrokenApplication()",
            "not_app = object()",
        ]),
        encoding="utf-8",
    )
    return module_path
