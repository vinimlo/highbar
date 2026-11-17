"""HB-43 end to end: the agent highbar started dies, and another process answers on its port.

The reviewer's shape (review of #57): another suite's agent binds the port after highbar
checked it was free. highbar's agent writes its hello at import, then dies on its own
bind, and the other agent answers every message without writing to this run's file. The
start command here plays both: it puts a stand-in agent that records nothing on the port
in the background, and the agent proper says hello and exits a second later.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.integration.test_report import SERVER, _free_port, _Project

DIES = """\
import sys, time
import highbar

hb = highbar.Highbar()  # its hello goes to HIGHBAR_TRACES_URL now
time.sleep(1.0)
sys.exit(1)  # as a server does when its port is taken
"""


@pytest.mark.timeout(40)
def test_hb43_guardrails_answered_by_another_process_are_not_a_pass(tmp_path: Path) -> None:
    project = _Project(tmp_path)
    (tmp_path / "dies.py").write_text(DIES, encoding="utf-8")
    port = _free_port()
    impostor = f"{sys.executable} {SERVER} --port {port} --no-traces --slow-chat 0.5"
    start = f"sh -c '{impostor} & exec {sys.executable} dies.py'"
    spec = _Project.agent(port, start=start) + _Project.suites(
        "function_calls: []", "function_calls: []", "function_calls: []"
    )
    (tmp_path / "agent.highbar.yaml").write_text(spec, encoding="utf-8")

    result = project.run("agent.highbar.yaml")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "the agent highbar started exited with code 1" in result.stdout
    tests = project.data()["specs"][0]["tests"]
    assert [t["status"] for t in tests] == ["error", "error", "error"]
