"""The CLI and the agent process, end to end (review findings P1–P5).

Each test writes a spec in a temporary directory and runs the real CLI against the
stand-in server in tests/fixtures/server.py.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from highbar.adapters.ports import accepts_connections

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


class _Project:
    """A temporary project with one spec, its own TMPDIR, and a free port."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = _free_port()
        self.tmp = root / "tmp"
        self.tmp.mkdir()

    def spec(
        self, *server_args: str, expect: str = "function_calls: []", start: str | None = None
    ) -> str:
        start = start or " ".join(
            [sys.executable, str(SERVER), "--port", str(self.port), *server_args]
        )
        (self.root / "agent.highbar.yaml").write_text(
            f"""\
agent:
  start: "{start}"
  endpoint: "http://127.0.0.1:{self.port}/chat"
  health: "http://127.0.0.1:{self.port}/health"
  startup_timeout: 5000
  settle_time: 50
suites:
  - name: S
    tests:
      - trigger: "hi"
        expect:
          {expect}
""",
            encoding="utf-8",
        )
        return "agent.highbar.yaml"

    def command(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "highbar.cli.main", "run", *args]

    @property
    def env(self) -> dict[str, str]:
        return {**os.environ, "TMPDIR": str(self.tmp), "COLUMNS": "200"}

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*args),
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def leftover_traces(self) -> list[Path]:
        return sorted(self.tmp.glob("highbar-traces-*"))


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


@pytest.mark.timeout(30)
def test_p2_cli_exits_2_when_port_is_busy(project: _Project) -> None:
    spec = project.spec()
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", project.port))
        squatter.listen()

        result = project.run(spec)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "already in use" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.timeout(30)
def test_p4_cli_shows_why_the_agent_died(project: _Project) -> None:
    result = project.run(project.spec("--exit-code", "3"))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "boom-marker" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.timeout(30)
def test_failed_run_points_to_agent_log(project: _Project) -> None:
    result = project.run(project.spec(expect='response: {contains: "nope"}'))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Agent log: .highbar/logs/agent-" in result.stdout


@pytest.mark.timeout(30)
def test_trace_file_removed_after_run(project: _Project) -> None:
    result = project.run(project.spec())

    assert result.returncode == 0, result.stdout + result.stderr
    assert project.leftover_traces() == []


@pytest.mark.timeout(30)
def test_keep_traces_keeps_the_file(project: _Project) -> None:
    result = project.run(project.spec(), "--keep-traces")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traces kept at" in result.stdout
    assert len(project.leftover_traces()) == 1


@pytest.mark.timeout(40)
def test_ctrl_c_stops_agent_and_cleans_up(project: _Project) -> None:
    """Ctrl-C mid-run: the agent's group goes down, the trace goes away, exit 130."""
    spec = project.spec("--slow-chat", "10")
    cli = subprocess.Popen(
        project.command(spec),
        cwd=project.root,
        env=project.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert _wait_until(lambda: accepts_connections("127.0.0.1", project.port), 10)
        time.sleep(0.5)  # let the CLI send the message the agent sits on

        cli.send_signal(signal.SIGINT)
        output, _ = cli.communicate(timeout=15)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == 130, output
    assert _wait_until(lambda: not accepts_connections("127.0.0.1", project.port), 6)
    assert project.leftover_traces() == []


@pytest.mark.timeout(30)
def test_typo_in_start_command_exits_2(project: _Project) -> None:
    result = project.run(project.spec(start="definitely-not-a-highbar-command"))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "could not run" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.timeout(30)
def test_uninstrumented_agent_exits_2(project: _Project) -> None:
    """An agent that writes no traces cannot pass a guardrail: nothing was checked."""
    result = project.run(project.spec("--no-traces"))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "nothing was written to" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert _wait_until(lambda: not accepts_connections("127.0.0.1", project.port), 6)


@pytest.mark.timeout(40)
@pytest.mark.parametrize(("sig", "code"), [(signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_termination_signal_stops_agent_and_cleans_up(
    project: _Project, sig: signal.Signals, code: int
) -> None:
    """`timeout`, a closed terminal or a cancelled CI job: the agent must not outlive the run."""
    spec = project.spec("--slow-chat", "10")
    cli = subprocess.Popen(
        project.command(spec),
        cwd=project.root,
        env=project.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert _wait_until(lambda: accepts_connections("127.0.0.1", project.port), 10)
        time.sleep(0.5)

        cli.send_signal(sig)
        output, _ = cli.communicate(timeout=15)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == code, output
    assert _wait_until(lambda: not accepts_connections("127.0.0.1", project.port), 6)
    assert project.leftover_traces() == []
