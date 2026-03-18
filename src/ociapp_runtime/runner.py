import subprocess
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _CommandExecutionError(Exception):
    """Raised when a runtime subprocess command fails."""


class _CommandResult(NamedTuple):
    """Captures the result of a subprocess command."""

    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


class _CommandRunner:
    """Executes subprocess commands for the runtime."""

    def run(
        self,
        args: "Sequence[str]",
        cwd: "Path | None" = None,
        timeout: float | None = None,
    ) -> _CommandResult:
        """Runs a subprocess command and captures its output."""

        try:
            completed = subprocess.run(
                list(args),
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            joined = " ".join(args)
            raise _CommandExecutionError(
                f"command timed out after {timeout}s: {joined}"
            ) from exc

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
