"""The CLI's output reads the same on GitHub's CI as on any other machine.

Typer forces color when ``GITHUB_ACTIONS`` is set, and the color codes split an option's
name into ``-`` and ``-max-cost``: a test that looked for the name passed everywhere but
on GitHub (the HB-34 tests of #25). ``tests/conftest.py`` turns that off for the suite and
for every highbar the suite starts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from highbar.cli.main import app

ERROR = "Invalid value for '--max-cost'"


def test_an_option_in_a_usage_error_reads_as_written(tmp_path: Path) -> None:
    """In this process, where typer read the environment when it was imported."""
    result = CliRunner().invoke(app, ["run", str(tmp_path), "--max-cost=abc"])

    assert result.exit_code == 2, result.output
    assert ERROR in result.output


def test_an_option_in_a_usage_error_reads_as_written_on_github(tmp_path: Path) -> None:
    """In a highbar the suite starts, with ``GITHUB_ACTIONS`` set as on GitHub's CI."""
    result = subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run", "--max-cost=abc"],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_ACTIONS": "true", "COLUMNS": "200"},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert ERROR in result.stdout + result.stderr
