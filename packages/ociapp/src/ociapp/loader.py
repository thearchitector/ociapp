import sys
from importlib import import_module, invalidate_caches
from typing import TYPE_CHECKING

from .application import Application
from .errors import ApplicationLoadError

if TYPE_CHECKING:
    from pydantic import BaseModel

__all__ = ["ApplicationLoadError", "load_application"]


def load_application(import_path: str) -> Application["BaseModel", "BaseModel"]:
    """Loads an application instance from an import path."""

    module_path, separator, attribute_name = import_path.partition(":")
    if not separator or not module_path or not attribute_name:
        raise ApplicationLoadError(
            "application import path must have the form 'pkg.module:attribute' and be formatted correctly"
        )

    try:
        invalidate_caches()
        sys.modules.pop(module_path, None)
        module = import_module(module_path)
    except Exception as exc:
        raise ApplicationLoadError(f"could not import module '{module_path}'") from exc

    if not hasattr(module, attribute_name):
        raise ApplicationLoadError(
            f"could not find attribute '{attribute_name}' in module '{module_path}'"
        )

    value = getattr(module, attribute_name)
    if not isinstance(value, Application):
        raise ApplicationLoadError(
            f"application import path '{import_path}' must resolve to an Application"
        )

    try:
        _ = value.request_model
        _ = value.response_model
    except TypeError as exc:
        raise ApplicationLoadError(str(exc)) from exc

    return value
