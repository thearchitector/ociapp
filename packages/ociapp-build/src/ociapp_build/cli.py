import argparse
from typing import TYPE_CHECKING

from .build import _build_project

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Builds the ociapp-build CLI parser."""

    parser = argparse.ArgumentParser(prog="ociapp-build")
    parser.add_argument("project_root")
    parser.add_argument("--output-dir")
    return parser


def main(argv: "Sequence[str] | None" = None) -> int:
    """Runs the ociapp-build CLI."""

    args = build_parser().parse_args(argv)
    _build_project(args.project_root, output_dir=args.output_dir)
    return 0
