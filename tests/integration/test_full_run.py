"""End-to-end integration test — runs Gauntlet against a real agent."""

import subprocess
import sys

import pytest


@pytest.mark.timeout(30)
def test_full_run() -> None:
    """Run gauntlet against the test agent fixture."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gauntlet.cli.main",
            "run",
            "tests/integration/fixtures/spec.gauntlet.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"Gauntlet failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "passed" in result.stdout.lower()


@pytest.mark.timeout(10)
def test_validate_command() -> None:
    """Validate a spec without running it."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gauntlet.cli.main",
            "validate",
            "tests/integration/fixtures/spec.gauntlet.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Valid" in result.stdout
