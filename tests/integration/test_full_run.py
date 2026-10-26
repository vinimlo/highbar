"""End-to-end integration test — runs Highbar against a real agent."""

import subprocess
import sys

import pytest


@pytest.mark.timeout(30)
def test_full_run() -> None:
    """Run highbar against the test agent fixture."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "highbar.cli.main",
            "run",
            "tests/integration/fixtures/spec.highbar.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Highbar failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "passed" in result.stdout.lower()


@pytest.mark.timeout(10)
def test_validate_command() -> None:
    """Validate a spec without running it."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "highbar.cli.main",
            "validate",
            "tests/integration/fixtures/spec.highbar.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Valid" in result.stdout


@pytest.mark.timeout(60)
def test_cli_runs_3_shows_3_of_3() -> None:
    """Every test of the fixture spec runs three times, and says so."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "highbar.cli.main",
            "run",
            "tests/integration/fixtures/spec.highbar.yaml",
            "--runs",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3/3" in result.stdout
