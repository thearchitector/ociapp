from typing import TYPE_CHECKING

import pytest
from ociapp import Application
from pydantic import BaseModel

if TYPE_CHECKING:
    from ociapp import Application


class EchoRequest(BaseModel):
    message: str


class EchoResponse(BaseModel):
    message: str


class EchoApplication(Application[EchoRequest, EchoResponse]):
    async def execute(self, request: EchoRequest) -> EchoResponse:
        return EchoResponse(message=request.message)


class FailingApplication(Application[EchoRequest, EchoResponse]):
    async def execute(self, request: EchoRequest) -> EchoResponse:
        raise RuntimeError(f"boom: {request.message}")


@pytest.fixture
def echo_app() -> "Application[EchoRequest, EchoResponse]":
    return EchoApplication()


@pytest.fixture
def failing_app() -> "Application[EchoRequest, EchoResponse]":
    return FailingApplication()
