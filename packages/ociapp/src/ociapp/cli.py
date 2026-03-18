import argparse
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from .loader import _load_application
from .protocol import SOCKET_PATH
from .server import _serve_application

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Builds the OCIApp command-line parser."""

    parser = argparse.ArgumentParser(prog="ociapp")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--app", required=True)
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    """Runs the OCIApp CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        app = _load_application(args.app)
        asyncio.run(_serve_application(app, socket_path=Path(SOCKET_PATH)))
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2
