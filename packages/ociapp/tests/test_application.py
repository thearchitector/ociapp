from collections.abc import Awaitable, Callable
from typing import cast

import ociapp
import pytest
from ociapp import Application
from pydantic import BaseModel, ValidationError


class DemoRequest(BaseModel):
    value: int


class DemoResponse(BaseModel):
    value: int


class DemoApplication(Application[DemoRequest, DemoResponse]):
    async def execute(self, request: DemoRequest) -> DemoResponse:
        return DemoResponse(value=request.value)


type ExecuteFn = Callable[[dict[str, object]], Awaitable[DemoResponse]]


@pytest.mark.asyncio
async def test_application_execute_validates_dict_input() -> None:
    app = DemoApplication()
    execute = cast("ExecuteFn", app.execute)

    result = await execute({"value": 7})

    assert result == DemoResponse(value=7)
    assert app.request_model is DemoRequest
    assert app.response_model is DemoResponse


@pytest.mark.asyncio
async def test_application_execute_raises_validation_errors() -> None:
    app = DemoApplication()
    execute = cast("ExecuteFn", app.execute)

    with pytest.raises(ValidationError, match="value"):
        await execute({"value": "bad"})


def test_package_root_exports_application_only() -> None:
    assert ociapp.__all__ == ["Application"]
    assert hasattr(ociapp, "Application")
    for name in (
        "ApplicationLoadError",
        "ErrorPayload",
        "PayloadCodecError",
        "ProtocolError",
        "load_application",
    ):
        assert not hasattr(ociapp, name)
