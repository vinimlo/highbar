"""HB-10 v1 end to end: a signal during agent.run's teardown waits for it to end."""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

import pytest

from tests.integration.test_report import PASSES, _Project, _start
from tests.integration.test_run_hooks import _events, _spec


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


SLOW_TEARDOWN = """\
import pathlib, time
log = pathlib.Path("events.log")
with log.open("a") as f:
    f.write("teardown start\\n")
time.sleep(2)
with log.open("a") as f:
    f.write("teardown done\\n")
"""


@pytest.mark.timeout(40)
@pytest.mark.parametrize(
    ("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)]
)
def test_hb10_a_signal_during_the_teardown_of_a_finished_run_waits_for_it(
    project: _Project, sig: signal.Signals, code: int
) -> None:
    """The run ended; the teardown is putting the flag back when the signal lands."""
    spec = _spec(project, PASSES)
    (project.root / "slow.py").write_text(SLOW_TEARDOWN, encoding="utf-8")
    path = project.root / spec
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"{sys.executable} hook.py teardown", f"{sys.executable} slow.py"
        ),
        encoding="utf-8",
    )

    cli = _start(project, spec)
    try:
        deadline = time.monotonic() + 20
        while "teardown start" not in _events(project):
            assert cli.poll() is None and time.monotonic() < deadline, _events(project)
            time.sleep(0.05)
        cli.send_signal(sig)
        cli.communicate(timeout=15)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == code
    assert _events(project) == ["setup", "agent.start", "teardown start", "teardown done"]


@pytest.mark.timeout(40)
def test_hb44_a_ctrl_c_during_the_teardown_says_why_highbar_waits(project: _Project) -> None:
    spec = _spec(project, PASSES)
    (project.root / "slow.py").write_text(SLOW_TEARDOWN, encoding="utf-8")
    path = project.root / spec
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"{sys.executable} hook.py teardown", f"{sys.executable} slow.py"
        ),
        encoding="utf-8",
    )

    cli = _start(project, spec)
    try:
        deadline = time.monotonic() + 20
        while "teardown start" not in _events(project):
            assert cli.poll() is None and time.monotonic() < deadline, _events(project)
            time.sleep(0.05)
        cli.send_signal(signal.SIGINT)
        output, _ = cli.communicate(timeout=15)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == 130
    assert output.count("Waiting for agent.run.teardown to put back what") == 1, output
