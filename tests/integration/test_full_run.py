"""End-to-end integration test — runs Highbar against a real agent.

Each run starts the fixture agent on a free port of its own: the spec says 8111, and on
the build node two suites at once both started it there, and one of them failed (HB-42).
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.quickstart import free_port

FIXTURES = Path(__file__).parent / "fixtures"
SPEC_PORT = 8111
SPEC_START = 'start: "python tests/integration/fixtures/agent.py"'


def _spec(directory: Path) -> Path:
    """The fixture spec, written in ``directory`` with the agent on a free port."""
    port = free_port()
    text = (FIXTURES / "spec.highbar.yaml").read_text(encoding="utf-8")
    # If the fixture stops saying these, this has to learn what it says now.
    assert str(SPEC_PORT) in text and SPEC_START in text
    text = text.replace(str(SPEC_PORT), str(port)).replace(
        SPEC_START, f'start: "{sys.executable} {FIXTURES / "agent.py"} {port}"'
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "spec.highbar.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.timeout(30)
def test_full_run(tmp_path: Path) -> None:
    """Run highbar against the test agent fixture."""
    result = subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run", str(_spec(tmp_path))],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
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
def test_cli_runs_3_shows_3_of_3(tmp_path: Path) -> None:
    """Every test of the fixture spec runs three times, and says so."""
    result = subprocess.run(
        [sys.executable, "-m", "highbar.cli.main", "run", str(_spec(tmp_path)), "--runs", "3"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3/3" in result.stdout


def test_hb42_each_full_run_listens_on_a_port_of_its_own(tmp_path: Path) -> None:
    """Two suites at once on the build node both started this agent on 8111 (HB-42)."""
    for name in ("a", "b"):
        spec = _spec(tmp_path / name).read_text(encoding="utf-8")

        assert "8111" not in spec
        (port,) = set(re.findall(r"localhost:(\d+)/", spec))
        assert f"agent.py {port}" in spec
