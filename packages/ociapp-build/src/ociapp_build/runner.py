import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class CommandExecutionError(Exception):
    """Raised when an OCIApp build command fails."""


@dataclass(slots=True, frozen=True)
class CommandResult:
    """Captures the output from a subprocess invocation."""

    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


class CommandRunner:
    """Runs external commands for OCIApp builds."""

    def run(self, args: "Sequence[str]", cwd: "Path | None" = None) -> CommandResult:
        """Executes a subprocess command."""

        completed = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
        result = CommandResult(
            args=tuple(args),
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
        if completed.returncode != 0:
            raise CommandExecutionError(
                f"command failed with exit code {completed.returncode}: {' '.join(args)}\n"
                f"{completed.stderr.strip()}"
            )

        return result
