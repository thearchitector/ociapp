import subprocess
from pathlib import Path

import pytest

from ociapp_runtime.runner import _CommandExecutionError, _CommandRunner


def test_command_runner_captures_output(tmp_path: Path) -> None:
    runner = _CommandRunner()

    result = runner.run(("bash", "-lc", "printf 'hello'"), cwd=tmp_path, timeout=1.0)

    assert result.stdout == "hello"
    assert result.returncode == 0


def test_command_runner_wraps_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="demo", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    runner = _CommandRunner()

    with pytest.raises(_CommandExecutionError, match="timed out"):
        runner.run(("demo",), timeout=1.0)
