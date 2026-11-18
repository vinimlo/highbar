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


GUARDRAIL_ONLY = (
    "suites:\n  - name: S\n    tests:\n"
    '      - {name: guardrail, trigger: "b", expect: {forbid: [refund]}}\n'
)


@pytest.mark.timeout(40)
def test_hb53_the_report_and_the_terminal_say_how_long_it_was_watched(
    project: _Project,
) -> None:
    port = _free_port()
    path = project.root / "agent.highbar.yaml"
    path.write_text(project.agent(port) + GUARDRAIL_ONLY, encoding="utf-8")

    result = project.run(path.name)

    assert result.returncode == 0, result.stdout + result.stderr
    guardrail = _guardrail(project)
    assert guardrail["watched_ms"] >= 2000, guardrail
    assert "Forbidden calls watched for 2." in result.stdout


@pytest.mark.timeout(40)
def test_hb53_a_spec_that_forbids_nothing_is_not_watched_longer(project: _Project) -> None:
    result = project.run(project.spec('response: {contains: "ok"}'))

    assert result.returncode == 0, result.stdout + result.stderr
    (test,) = project.data()["specs"][0]["tests"]
    assert test["watched_ms"] is None
    assert "watched" not in result.stdout
    # It waits settle_time after the last test, as before, and not agent.watch.
    assert project.data()["duration_ms"] < 2000


@pytest.mark.timeout(40)
def test_hb53_agent_watch_sets_the_horizon(project: _Project) -> None:
    """A call 2.5 s after the reply is past the default 2 s; watch: 3500 sees it."""
    port = _free_port()
    path = project.root / "agent.highbar.yaml"
    path.write_text(
        project.agent(port, "--late-tool", "refund:2.5", extra="  watch: 3500\n")
        + GUARDRAIL_LAST,
        encoding="utf-8",
    )

    result = project.run(path.name)

    assert result.returncode == 1, result.stdout + result.stderr
    guardrail = _guardrail(project)
    assert guardrail["status"] == "failed", guardrail
    assert guardrail["watched_ms"] >= 3500
