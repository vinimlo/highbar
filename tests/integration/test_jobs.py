"""HB-8 end to end: Ctrl-C under --jobs 2 stops the run as it does under --jobs 1."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_report import PASSES, _Project, _until_first_result
from tests.integration.test_run_hooks import _events, _spec


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


@pytest.mark.timeout(40)
def test_hb8_ctrl_c_under_jobs_2_exits_130_with_the_agent_down_and_the_teardown_run(
    project: _Project,
) -> None:
    spec = _spec(project, PASSES, PASSES, PASSES, PASSES, PASSES, args=("--slow-chat", "1"))

    cli = subprocess.Popen(
        project.command(spec, "--jobs", "2", "--report", f"json={project.report}"),
        cwd=project.root,
        env=project.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _until_first_result(cli)
        cli.send_signal(signal.SIGINT)
        output, _ = cli.communicate(timeout=20)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == 130, output
    assert _events(project) == ["setup", "agent.start", "teardown"]
    data = project.data()
    assert data["stopped"]["reason"] == "interrupted"
    assert data["exit_code"] == 130
