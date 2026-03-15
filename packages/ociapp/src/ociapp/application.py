from abc import ABC, abstractmethod
from functools import cached_property
from inspect import Signature, signature
from typing import cast

from pydantic import BaseModel, validate_call


class Application[RequestT: BaseModel, ResponseT: BaseModel](ABC):
    """Defines the sandbox-side OCIApp execution contract."""

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        execute_method = cls.__dict__.get("execute")
        if execute_method is None:
            return

        setattr(cls, "execute", validate_call(validate_return=True)(execute_method))

    @property
    def request_model(self) -> type[RequestT]:
        """Returns the Pydantic model used for request validation."""

        return cast(type[RequestT], self._resolve_model("request"))

    @property
    def response_model(self) -> type[ResponseT]:
        """Returns the Pydantic model used for response validation."""

        return cast(type[ResponseT], self._resolve_model("response"))

    @cached_property
    def _execute_signature(self) -> Signature:
        return signature(self.execute)

    @abstractmethod
    async def execute(self, request: RequestT) -> ResponseT:
        """Executes a single validated request."""

    def _resolve_model(self, kind: str) -> type[BaseModel]:
        if kind == "request":
            parameters = tuple(self._execute_signature.parameters.values())
            if len(parameters) != 1:
                raise TypeError(
                    "Application.execute must accept exactly one request parameter"
                )
            annotation = parameters[0].annotation
            field_name = "request"
        else:
            annotation = self._execute_signature.return_annotation
            field_name = "response"

        if annotation is Signature.empty:
            raise TypeError(
                f"Application.execute must annotate its {field_name} with a pydantic.BaseModel subtype"
            )
        if not isinstance(annotation, type) or not issubclass(annotation, BaseModel):
            raise TypeError(
                f"Application.execute must annotate its {field_name} with a pydantic.BaseModel subtype"
            )

        return annotation
