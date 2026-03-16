import argparse
import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from .loader import load_application
from .protocol import DEFAULT_SOCKET_PATH
from .server import serve_application

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Builds the OCIApp command-line parser."""

    parser = argparse.ArgumentParser(prog="ociapp")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--app", required=True)
    serve_parser.add_argument("--socket-path", default=DEFAULT_SOCKET_PATH)
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    """Runs the OCIApp CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        app = load_application(args.app)
        asyncio.run(serve_application(app, socket_path=Path(args.socket_path)))
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2
