"""The quickstart, end to end: `highbar init`, then `highbar run`, with the real CLI.

The agent is examples/quickstart/agent.py, started by the spec on its own port.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _highbar(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", *args],
        cwd=cwd,
        env={**os.environ, "COLUMNS": "200"},
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def quickstart(tmp_path: Path) -> Path:
    result = _highbar(tmp_path, "init")
    assert result.returncode == 0, result.stdout + result.stderr
    return tmp_path


@pytest.mark.timeout(120)
def test_quickstart_runs_green(quickstart: Path) -> None:
    result = _highbar(quickstart, "run")

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "4 passed" in output
    assert "3/3" in output  # the test with runs: 3


@pytest.mark.timeout(120)
def test_quickstart_guardrails_fire_against_a_misbehaving_agent(quickstart: Path) -> None:
    """The README's "break it" step: the same spec, the agent's guardrails off."""
    agent = quickstart / "agent.py"
    source = agent.read_text(encoding="utf-8")
    assert source.count("GUARDRAILS = True") == 1
    agent.write_text(source.replace("GUARDRAILS = True", "GUARDRAILS = False"), encoding="utf-8")

    result = _highbar(quickstart, "run")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "forbidden tool 'delete_account' was called" in output
    assert "Response matches forbidden pattern" in output
    assert "2 passed | 2 failed" in output
