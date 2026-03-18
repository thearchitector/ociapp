from pathlib import Path

import pytest
from ociapp import Application
from ociapp.cli import main
from ociapp.errors import ApplicationLoadError
from ociapp.loader import _load_application
from ociapp.protocol import SOCKET_PATH
from pydantic import BaseModel


class EchoRequest(BaseModel):
    value: str


class EchoResponse(BaseModel):
    value: str


class EchoApplication(Application[EchoRequest, EchoResponse]):
    async def execute(self, request: EchoRequest) -> EchoResponse:
        return EchoResponse(value=request.value)


def test_load_application_import_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_dir = tmp_path / "sample_app"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text(
        """
from pydantic import BaseModel

from ociapp import Application


class EchoRequest(BaseModel):
    value: str


class EchoResponse(BaseModel):
    value: str


class EchoApplication(Application[EchoRequest, EchoResponse]):
    async def execute(self, request: EchoRequest) -> EchoResponse:
        return EchoResponse(value=request.value)


app = EchoApplication()
""".strip()
        + "\n"
    )
    monkeypatch.syspath_prepend(tmp_path)

    app = _load_application("sample_app:app")

    assert isinstance(app, Application)
    assert app.request_model.__name__ == "EchoRequest"


def test_load_application_rejects_invalid_target() -> None:
    with pytest.raises(ApplicationLoadError, match="formatted"):
        _load_application("missing-separator")


def test_cli_serve_loads_import_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    app = EchoApplication()

    def fake_load(import_path: str) -> Application[EchoRequest, EchoResponse]:
        captured["import_path"] = import_path
        return app

    async def fake_serve(
        loaded_app: Application[EchoRequest, EchoResponse], *, socket_path: Path
    ) -> None:
        captured["app"] = loaded_app
        captured["socket_path"] = socket_path

    monkeypatch.setattr("ociapp.cli._load_application", fake_load)
    monkeypatch.setattr("ociapp.cli._serve_application", fake_serve)

    result = main(["serve", "--app", "sample_app:app"])

    assert result == 0
    assert captured["import_path"] == "sample_app:app"
    assert captured["app"] is app
    assert captured["socket_path"] == Path(SOCKET_PATH)
