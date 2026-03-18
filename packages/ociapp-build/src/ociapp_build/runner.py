import subprocess
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _CommandExecutionError(Exception):
    """Raised when an OCIApp build command fails."""


class _CommandResult(NamedTuple):
    """Captures the output from a subprocess invocation."""

    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


class _CommandRunner:
    """Runs external commands for OCIApp builds."""

    def run(self, args: "Sequence[str]", cwd: "Path | None" = None) -> _CommandResult:
        """Executes a subprocess command."""

        completed = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
        result = _CommandResult(
            args=tuple(args),
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
        if completed.returncode != 0:
            raise _CommandExecutionError(
                f"command failed with exit code {completed.returncode}: {' '.join(args)}\n"
                f"{completed.stderr.strip()}"
            )

        return result
