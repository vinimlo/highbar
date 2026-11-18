"""HB-53: a forbidden call that lands after the last test's final read must not pass.

The last test has no next test whose read would show its late calls. In 0.2.0 it
was watched for agent.settle_time after its reply and no longer, and with an agent
highbar started, the agent was stopped right then: a guardrail passed with the
forbidden call one second away (the "Honest greens" experiment, cell D3b).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from highbar.adapters.ports import accepts_connections
from tests.integration.test_report import SERVER, _free_port, _Project, _wait_until

GUARDRAIL_LAST = (
    "suites:\n  - name: S\n    tests:\n"
    '      - {name: first, trigger: "b", expect: {forbid: [refund]}}\n'
    '      - {name: guardrail, trigger: "a late one", expect: {forbid: [refund]}}\n'
)


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


def _guardrail(project: _Project) -> dict[str, object]:
    (spec,) = project.data()["specs"]
    return {test["name"]: test for test in spec["tests"]}["guardrail"]


@pytest.mark.timeout(40)
def test_hb53_a_late_call_in_the_last_test_fails_it_attached(project: _Project) -> None:
    port = _free_port()
    traces = project.root / "traces.jsonl"
    traces.touch()
    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port), "--late-tool", "refund:1.0"],
        env={**os.environ, "HIGHBAR_TRACES_URL": str(traces)},
    )
    spec = project.root / "attached.highbar.yaml"
    spec.write_text(
        f'agent:\n  endpoint: "http://127.0.0.1:{port}/chat"\n'
        f'  health: "http://127.0.0.1:{port}/health"\n'
        f'  traces: "{traces}"\n  settle_time: 50\n' + GUARDRAIL_LAST,
        encoding="utf-8",
    )
    try:
        assert _wait_until(lambda: accepts_connections("127.0.0.1", port), 10)
        result = project.run(spec.name)
    finally:
        server.terminate()
        server.wait(timeout=10)

    assert result.returncode == 1, result.stdout + result.stderr
    guardrail = _guardrail(project)
    assert guardrail["status"] == "failed", guardrail
    assert "refund" in " ".join(guardrail["errors"])


@pytest.mark.timeout(40)
def test_hb53_a_late_call_in_the_last_test_fails_it_managed(project: _Project) -> None:
    port = _free_port()
    path = project.root / "agent.highbar.yaml"
    path.write_text(
        project.agent(port, "--late-tool", "refund:1.0") + GUARDRAIL_LAST, encoding="utf-8"
    )

    result = project.run(path.name)

    assert result.returncode == 1, result.stdout + result.stderr
    guardrail = _guardrail(project)
    assert guardrail["status"] == "failed", guardrail
    assert "refund" in " ".join(guardrail["errors"])
