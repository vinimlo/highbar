"""HB-10 end to end: ``agent.run`` around a real run of the CLI.

The teardown must run on every way out, a signal included, or a flag the setup turned
on stays on in a shared bot after the run.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

import pytest

from tests.integration.test_report import (
    PASSES,
    _free_port,
    _Project,
    _start,
    _until_first_result,
)

HOOK = """\
import pathlib, sys
with pathlib.Path("events.log").open("a") as f:
    f.write(sys.argv[1] + "\\n")
sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
"""


def _spec(project: _Project, *expects: str, setup: str = "setup", **agent: object) -> str:
    """A managed agent whose start, run setup and run teardown each write to events.log."""
    (project.root / "hook.py").write_text(HOOK, encoding="utf-8")
    port = _free_port()
    args = agent.pop("args", ())
    server = " ".join(
        [sys.executable, str(Path(__file__).parents[1] / "fixtures" / "server.py")]
        + ["--port", str(port), *args]  # type: ignore[list-item]
    )
    start = f"{sys.executable} hook.py agent.start && exec {server}"
    text = (
        _Project.agent(port, start=f"sh -c '{start}'")
        + f'  run:\n    setup: "{sys.executable} hook.py {setup}"\n'
        + f'    teardown: "{sys.executable} hook.py teardown"\n'
        + _Project.suites(*expects)
    )
    (project.root / "agent.highbar.yaml").write_text(text, encoding="utf-8")
    return "agent.highbar.yaml"


def _events(project: _Project) -> list[str]:
    path = project.root / "events.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


@pytest.mark.timeout(30)
def test_hb10_setup_before_the_agent_starts_and_teardown_after_the_run(
    project: _Project,
) -> None:
    result = project.run(_spec(project, PASSES))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _events(project) == ["setup", "agent.start", "teardown"]


@pytest.mark.timeout(40)
@pytest.mark.parametrize(
    ("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)]
)
def test_hb10_teardown_runs_when_a_signal_stops_the_run(
    project: _Project, sig: signal.Signals, code: int
) -> None:
    spec = _spec(project, PASSES, PASSES, PASSES, args=("--slow-chat", "1"))

    cli = _start(project, spec)
    try:
        _until_first_result(cli)
        cli.send_signal(sig)
        cli.communicate(timeout=15)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == code
    assert _events(project) == ["setup", "agent.start", "teardown"]


@pytest.mark.timeout(60)
def test_hb10_teardown_runs_after_a_second_ctrl_c_while_the_agent_stops(
    project: _Project,
) -> None:
    """An agent that ignores SIGTERM keeps highbar in its grace before SIGKILL; a second
    Ctrl-C lands there (review of #54)."""
    spec = _spec(project, PASSES, PASSES, PASSES, args=("--slow-chat", "1", "--ignore-term"))

    cli = _start(project, spec)
    try:
        _until_first_result(cli)
        cli.send_signal(signal.SIGINT)
        time.sleep(1)
        cli.send_signal(signal.SIGINT)
        cli.communicate(timeout=30)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == 130
    assert _events(project) == ["setup", "agent.start", "teardown"]


@pytest.mark.timeout(30)
def test_hb10_a_setup_that_fails_exits_2_before_the_agent_starts(project: _Project) -> None:
    result = project.run(_spec(project, PASSES, setup="setup 1"))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "agent.run.setup exited with code 1" in result.stdout
    assert _events(project) == ["setup"]
    data = project.data()
    assert data["stopped"]["reason"] == "run_setup_failed"
    assert data["exit_code"] == 2
