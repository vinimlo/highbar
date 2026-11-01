"""`highbar run --report json=<file>`, through the real CLI (spec D19, D20).

The file has to hold on every way out of a run, and has to agree with the process.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from highbar.adapters.ports import accepts_connections
from tests.unit.reporters.test_json_report import _closed, _schema

SERVER = Path(__file__).resolve().parents[1] / "fixtures" / "server.py"
PASSES = 'response: {contains: "ok"}'
FAILS = 'response: {contains: "absent"}'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class _Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.report = root / "report.json"

    @staticmethod
    def agent(port: int, *server_args: str, start: str | None = None) -> str:
        start = start or " ".join([sys.executable, str(SERVER), "--port", str(port), *server_args])
        return (
            f'agent:\n  start: "{start}"\n  endpoint: "http://127.0.0.1:{port}/chat"\n'
            f'  health: "http://127.0.0.1:{port}/health"\n'
            "  startup_timeout: 5000\n  settle_time: 50\n"
        )

    @staticmethod
    def suites(*expects: str) -> str:
        tests = "".join(
            f'      - trigger: "message {i}"\n        expect: {{{expect}}}\n'
            for i, expect in enumerate(expects)
        )
        return f"suites:\n  - name: S\n    tests:\n{tests}"

    def spec(self, *expects: str, name: str = "agent.highbar.yaml", **agent: Any) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        port = agent.pop("port", None) or _free_port()
        args = agent.pop("args", ())
        path.write_text(self.agent(port, *args, **agent) + self.suites(*expects), encoding="utf-8")
        return name

    def command(self, *args: str) -> list[str]:
        return [sys.executable, "-m", "highbar.cli.main", "run", *args]

    @property
    def env(self) -> dict[str, str]:
        return {**os.environ, "COLUMNS": "200"}

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*args, "--report", f"json={self.report}"),
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def data(self) -> dict[str, Any]:
        return json.loads(self.report.read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


@pytest.mark.timeout(30)
@pytest.mark.parametrize(
    ("expect", "server_args", "code"),
    [(PASSES, (), 0), (FAILS, (), 1), ("function_calls: []", ("--no-traces",), 2)],
)
def test_j10_exit_code_in_the_file_is_the_process_code(
    project: _Project, expect: str, server_args: tuple[str, ...], code: int
) -> None:
    result = project.run(project.spec(expect, args=server_args))

    assert result.returncode == code, result.stdout + result.stderr
    assert project.data()["exit_code"] == code


@pytest.mark.timeout(30)
def test_a_full_run_is_complete_and_agrees_with_the_terminal(project: _Project) -> None:
    result = project.run(project.spec(PASSES, FAILS))

    assert "1 passed | 1 failed" in result.stdout
    assert str(project.report) in result.stdout
    data = project.data()
    assert data["complete"] is True
    assert data["stopped"] is None
    assert (data["summary"]["passed"], data["summary"]["failed"]) == (1, 1)
    (spec,) = data["specs"]
    assert spec["path"] == "agent.highbar.yaml"
    assert [test["status"] for test in spec["tests"]] == ["passed", "failed"]
    assert [test["id"] for test in spec["tests"]] == ["S/message 0", "S/message 1"]
    (execution,) = spec["tests"][1]["phrasings"][0]["executions"]
    (turn,) = execution["turns"]
    assert (turn["trigger"], turn["response"]) == ("message 1", "ok")
    assert "absent" in execution["errors"][0]


@pytest.mark.timeout(30)
def test_ac14_a_run_without_usage_has_no_totals_and_no_warning(project: _Project) -> None:
    project.run(project.spec(PASSES))

    data = project.data()
    assert data["totals"] is None
    (spec,) = data["specs"]
    assert (spec["totals"], spec["warnings"]) == (None, [])
    assert spec["suites"] == [{"name": "S", "totals": None}]
    assert spec["tests"][0]["budget"] is None
    (turn,) = spec["tests"][0]["phrasings"][0]["executions"][0]["turns"]
    assert (turn["usage"], turn["budget"]) == (None, None)


@pytest.mark.timeout(30)
def test_ac4_two_suites_add_up_at_every_level_and_in_the_terminal(project: _Project) -> None:
    (project.root / "two.highbar.yaml").write_text(
        project.agent(_free_port(), "--cost", "0.01")
        + "suites:\n  - name: A\n    tests:\n"
        + f'      - {{trigger: "one", expect: {{{PASSES}}}}}\n'
        + f'      - {{trigger: "two", expect: {{{PASSES}}}}}\n'
        + "  - name: B\n    tests:\n"
        + f'      - {{trigger: "three", expect: {{{PASSES}}}}}\n',
        encoding="utf-8",
    )

    result = project.run("two.highbar.yaml")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "300 input tokens | 30 output tokens | 0.03 USD" in result.stdout
    data = project.data()
    jsonschema.Draft202012Validator(_closed(_schema())).validate(data)
    assert data["totals"]["input_tokens"] == 300
    assert data["totals"]["cost"] == {"USD": 0.03}
    assert data["totals"]["complete"] is True
    (spec,) = data["specs"]
    assert spec["totals"] == data["totals"]
    first, second = spec["suites"]
    assert (first["name"], first["totals"]["cost"]) == ("A", {"USD": 0.02})
    assert (second["name"], second["totals"]["cost"]) == ("B", {"USD": 0.01})
    one = spec["tests"][0]
    assert one["totals"]["output_tokens"] == 10
    (execution,) = one["phrasings"][0]["executions"]
    assert execution["totals"]["turns"] == 1
    assert execution["turns"][0]["usage"] == {
        "model": None,
        "provider": None,
        "input_tokens": 100,
        "output_tokens": 10,
        "cached_input_tokens": None,
        "cost": 0.01,
        "currency": "USD",
        "llm_calls": None,
    }


@pytest.mark.timeout(40)
def test_ac12_max_cost_stops_at_the_end_of_the_test_in_progress(project: _Project) -> None:
    spec = project.spec(PASSES, PASSES, PASSES, args=("--cost", "0.01"))

    result = project.run(spec, "--max-cost", "0.015")

    assert result.returncode == 3, result.stdout + result.stderr
    assert "--max-cost 0.015" in result.stdout
    data = project.data()
    jsonschema.Draft202012Validator(_closed(_schema())).validate(data)
    assert data["exit_code"] == 3
    assert data["complete"] is False
    assert data["stopped"]["reason"] == "budget_exceeded"
    assert "0.02 USD" in data["stopped"]["message"]
    (stopped,) = data["specs"]
    assert stopped["complete"] is False
    assert [test["id"] for test in stopped["tests"]] == ["S/message 0", "S/message 1"]
    assert [test["status"] for test in stopped["tests"]] == ["passed", "passed"]
    assert data["summary"]["total"] == 2
    assert data["totals"]["cost"] == {"USD": 0.02}


@pytest.mark.timeout(40)
def test_ac12_the_ceiling_is_the_whole_runs_across_specs(project: _Project) -> None:
    project.spec(PASSES, PASSES, name="specs/a.highbar.yaml", args=("--cost", "0.01"))
    project.spec(PASSES, name="specs/b.highbar.yaml", args=("--cost", "0.01"))
    project.spec(PASSES, name="specs/c.highbar.yaml", args=("--cost", "0.01"))

    result = project.run("specs", "--max-cost", "0.025")

    assert result.returncode == 3, result.stdout + result.stderr
    data = project.data()
    assert [spec["path"] for spec in data["specs"]] == [
        "specs/a.highbar.yaml",
        "specs/b.highbar.yaml",
    ]
    assert [spec["complete"] for spec in data["specs"]] == [True, False]
    assert data["totals"]["cost"] == {"USD": 0.03}


@pytest.mark.timeout(30)
def test_ac12_under_the_ceiling_the_exit_code_is_the_tests(project: _Project) -> None:
    result = project.run(project.spec(PASSES, args=("--cost", "0.01")), "--max-cost", "1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert project.data()["stopped"] is None


@pytest.mark.timeout(30)
def test_ac12_a_failed_test_does_not_hide_the_reason_the_run_stopped(project: _Project) -> None:
    spec = project.spec(FAILS, PASSES, args=("--cost", "0.01"))

    result = project.run(spec, "--max-cost", "0.005")

    assert result.returncode == 3, result.stdout + result.stderr
    data = project.data()
    assert data["stopped"]["reason"] == "budget_exceeded"
    assert data["specs"][0]["tests"][0]["status"] == "failed"


@pytest.mark.timeout(30)
@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_ac12_a_ceiling_that_is_not_one_stops_before_any_message(
    project: _Project, value: str
) -> None:
    spec = project.spec(PASSES, start="touch agent-started")

    result = project.run(spec, f"--max-cost={value}")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--max-cost" in result.stdout
    assert project.data()["stopped"]["reason"] == "usage_error"
    assert not (project.root / "agent-started").exists()


@pytest.mark.timeout(30)
def test_ac5_a_cost_nobody_reported_is_incomplete_in_the_file_and_the_terminal(
    project: _Project,
) -> None:
    """Two specs, so the run adds a turn with a cost to a turn without usage."""
    project.spec(PASSES, name="specs/a.highbar.yaml", args=("--cost", "0.01"))
    project.spec(PASSES, name="specs/b.highbar.yaml")

    result = project.run("specs")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "incomplete" in result.stdout
    totals = project.data()["totals"]
    assert totals["cost"] == {"USD": 0.01}
    assert totals["complete"] is False
    assert totals["warnings"] == ["1 of 2 turns with no usage"]


@pytest.mark.timeout(30)
def test_ac9_a_budget_without_usage_is_an_error_in_the_file(project: _Project) -> None:
    result = project.run(project.spec("budget: {max_cost: 1}"))

    assert result.returncode == 1, result.stdout + result.stderr
    (test,) = project.data()["specs"][0]["tests"]
    assert test["status"] == "error"
    assert test["budget"] == {"max_input_tokens": None, "max_cost": 1}
    assert "no usage" in test["errors"][0]


@pytest.mark.timeout(30)
def test_q1_usage_with_no_test_context_cannot_pass_a_budget(project: _Project) -> None:
    spec = project.spec("budget: {max_cost: 1}", args=("--cost", "0.1", "--stray-cost", "9"))

    result = project.run(spec)

    assert result.returncode == 1, result.stdout + result.stderr
    data = project.data()
    jsonschema.Draft202012Validator(_closed(_schema())).validate(data)
    (test,) = data["specs"][0]["tests"]
    assert test["status"] == "error"
    assert "cannot tell which test" in test["errors"][0]
    assert data["totals"]["cost"] == {"USD": 0.1}
    assert data["totals"]["complete"] is False
    assert "no test context" in result.stdout


@pytest.mark.timeout(30)
def test_j5_invalid_spec_reports_why(project: _Project) -> None:
    (project.root / "bad.highbar.yaml").write_text("agent: {endpoint: 1}\n", encoding="utf-8")

    result = project.run("bad.highbar.yaml")

    assert result.returncode == 2
    data = project.data()
    assert data["exit_code"] == 2
    assert data["complete"] is False
    assert data["stopped"]["reason"] == "spec_invalid"
    assert "bad.highbar.yaml" in data["stopped"]["message"]
    assert data["specs"] == []


@pytest.mark.timeout(30)
def test_j5_hooks_that_do_not_load(project: _Project) -> None:
    spec = project.spec(PASSES)
    path = project.root / spec
    text = path.read_text(encoding="utf-8")
    hook = "  session:\n    setup: {python: 'no_such_module_here:setup'}\n"
    path.write_text(text.replace("suites:", hook + "suites:", 1), encoding="utf-8")

    result = project.run(spec)

    assert result.returncode == 2, result.stdout + result.stderr
    assert project.data()["stopped"]["reason"] == "hooks_invalid"
    assert "no_such_module_here" in project.data()["stopped"]["message"]


@pytest.mark.timeout(30)
def test_j5_no_specs(project: _Project) -> None:
    (project.root / "empty").mkdir()

    result = project.run("empty")

    assert result.returncode == 2
    assert project.data()["stopped"]["reason"] == "no_specs"


@pytest.mark.timeout(30)
def test_j6_unwritable_report_exits_2_before_any_message(project: _Project) -> None:
    spec = project.spec(PASSES, start="touch agent-started")
    locked = project.root / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    project.report = locked / "report.json"

    try:
        result = project.run(spec)
    finally:
        locked.chmod(0o700)

    assert result.returncode == 2
    assert "cannot write the report" in result.stdout
    assert not (project.root / "agent-started").exists()


@pytest.mark.timeout(30)
def test_m2_runs_0_leaves_no_older_green_behind(project: _Project) -> None:
    spec = project.spec(PASSES)
    project.report.write_text('{"exit_code": 0}', encoding="utf-8")

    result = project.run(spec, "--runs", "0")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--runs" in result.stdout
    data = project.data()
    assert data["exit_code"] == 2
    assert data["stopped"]["reason"] == "usage_error"


def _start(project: _Project, spec: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        project.command(spec, "--report", f"json={project.report}"),
        cwd=project.root,
        env=project.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _until_first_result(cli: subprocess.Popen[str]) -> None:
    """Read the CLI's output until its first test passed: the second is on its way."""
    assert cli.stdout is not None
    for line in cli.stdout:
        if "✓" in line:
            return
    raise AssertionError("the CLI ended before its first result")


@pytest.mark.timeout(40)
@pytest.mark.parametrize(
    ("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)]
)
def test_j7_interrupted_run_keeps_finished_tests(
    project: _Project, sig: signal.Signals, code: int
) -> None:
    spec = project.spec(PASSES, PASSES, PASSES, args=("--slow-chat", "1"))

    cli = _start(project, spec)
    try:
        _until_first_result(cli)
        cli.send_signal(sig)
        cli.communicate(timeout=15)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == code
    data = project.data()
    assert data["exit_code"] == code
    assert data["complete"] is False
    assert data["stopped"]["reason"] == "interrupted"
    (spec_result,) = data["specs"]
    assert spec_result["complete"] is False
    # The test in progress is not a result.
    assert [test["id"] for test in spec_result["tests"]] == ["S/message 0"]
    assert data["summary"]["total"] == 1


@pytest.mark.timeout(40)
def test_m3_ctrl_c_before_the_first_spec_is_in_the_file(project: _Project) -> None:
    spec = project.spec(PASSES)
    path = project.root / spec
    text = path.read_text(encoding="utf-8")
    hook = "  session:\n    setup: {python: 'slow_hook:setup'}\n"
    path.write_text(text.replace("suites:", hook + "suites:", 1), encoding="utf-8")
    loading = project.root / "loading"
    (project.root / "slow_hook.py").write_text(
        f"import pathlib, time\npathlib.Path({str(loading)!r}).touch()\ntime.sleep(25)\n",
        encoding="utf-8",
    )

    cli = _start(project, spec)
    try:
        assert _wait_until(loading.exists, 15)
        cli.send_signal(signal.SIGINT)
        cli.communicate(timeout=15)
    finally:
        if cli.poll() is None:
            cli.kill()

    assert cli.returncode == 130
    data = project.data()
    assert data["exit_code"] == 130
    assert data["complete"] is False
    assert data["stopped"]["reason"] == "interrupted"


def _turns_written(traces: Path) -> int:
    return traces.read_text(encoding="utf-8").count('"type": "turn"')


@pytest.mark.timeout(40)
def test_c1_ctrl_c_keeps_a_late_failure_highbar_already_read(project: _Project) -> None:
    """The next test's read found the call, and that test never ends to apply it."""
    port = _free_port()
    traces = project.root / "traces.jsonl"
    traces.touch()
    server = subprocess.Popen(
        [
            sys.executable,
            str(SERVER),
            "--port",
            str(port),
            "--slow-chat",
            "0.4",
            "--late-tool",
            "refund:0.2",
        ],  # fmt: skip
        env={**os.environ, "HIGHBAR_TRACES_URL": str(traces)},
    )
    spec = project.root / "attached.highbar.yaml"
    spec.write_text(
        f'agent:\n  endpoint: "http://127.0.0.1:{port}/chat"\n'
        f'  health: "http://127.0.0.1:{port}/health"\n'
        f'  traces: "{traces}"\n  settle_time: 50\n'
        "suites:\n  - name: S\n    tests:\n"
        '      - {name: guardrail, trigger: "a late one", expect: {forbid: [refund]}}\n'
        '      - {name: next, trigger: "b", runs: 3, expect: {forbid: [refund]}}\n',
        encoding="utf-8",
    )

    try:
        assert _wait_until(lambda: accepts_connections("127.0.0.1", port), 10)
        cli = _start(project, spec.name)
        # A third turn means the read of the second test's first run came before it.
        assert _wait_until(lambda: _turns_written(traces) >= 3, 15)
        cli.send_signal(signal.SIGINT)
        output, _ = cli.communicate(timeout=15)
    finally:
        server.terminate()
        server.wait(timeout=10)

    assert cli.returncode == 130, output
    assert "now fails" in output
    data = project.data()
    assert data["exit_code"] == 130
    (test,) = data["specs"][0]["tests"]
    assert (test["id"], test["status"]) == ("S/guardrail", "failed")
    assert "refund" in test["errors"][0]
    assert (data["summary"]["passed"], data["summary"]["failed"]) == (0, 1)


@pytest.mark.timeout(40)
def test_j4_killed_run_leaves_an_unfinished_report(project: _Project) -> None:
    """SIGKILL ends highbar with no chance to write: an older green must not survive."""
    port = _free_port()
    traces = project.root / "traces.jsonl"
    traces.touch()
    server = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(port), "--slow-chat", "1"],
        env={**os.environ, "HIGHBAR_TRACES_URL": str(traces)},
    )
    spec = project.root / "attached.highbar.yaml"
    spec.write_text(
        f'agent:\n  endpoint: "http://127.0.0.1:{port}/chat"\n'
        f'  health: "http://127.0.0.1:{port}/health"\n  settle_time: 50\n'
        + project.suites(PASSES, PASSES, PASSES),
        encoding="utf-8",
    )
    project.report.write_text('{"exit_code": 0, "complete": true}', encoding="utf-8")

    cli = _start(project, spec.name)
    try:
        assert _wait_until(lambda: accepts_connections("127.0.0.1", port), 10)
        _until_first_result(cli)
        cli.kill()
        cli.wait(timeout=10)
    finally:
        if cli.poll() is None:
            cli.kill()
        server.terminate()
        server.wait(timeout=10)

    data = project.data()
    assert data["exit_code"] is None
    assert data["complete"] is False


@pytest.mark.timeout(40)
def test_j8_agent_that_fails_on_the_second_spec(project: _Project) -> None:
    project.spec(PASSES, name="specs/a.highbar.yaml")
    project.spec(PASSES, name="specs/b.highbar.yaml", start="definitely-not-a-highbar-command")

    result = project.run("specs")

    assert result.returncode == 2, result.stdout + result.stderr
    data = project.data()
    assert data["exit_code"] == 2
    assert data["complete"] is False
    assert data["stopped"]["reason"] == "agent_start_failed"
    first, second = data["specs"]
    assert (first["complete"], first["summary"]["passed"]) == (True, 1)
    assert (second["complete"], second["tests"]) == (False, [])


@pytest.mark.timeout(40)
def test_report_is_one_file_for_several_specs(project: _Project) -> None:
    project.spec(PASSES, name="specs/a.highbar.yaml")
    project.spec(FAILS, name="specs/b.highbar.yaml")

    result = project.run("specs")

    assert result.returncode == 1
    data = project.data()
    assert data["complete"] is True
    assert [spec["path"] for spec in data["specs"]] == [
        "specs/a.highbar.yaml",
        "specs/b.highbar.yaml",
    ]
    assert (data["summary"]["total"], data["summary"]["failed"]) == (2, 1)
    assert all(spec["sha256"] for spec in data["specs"])


@pytest.mark.timeout(30)
@pytest.mark.parametrize("value", ["xml=out.xml", "out.json", "json=", "=out.json"])
def test_report_needs_a_known_format_and_a_file(project: _Project, value: str) -> None:
    spec = project.spec(PASSES, start="touch agent-started")

    result = subprocess.run(
        project.command(spec, "--report", value),
        cwd=project.root,
        env=project.env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert "json=<file>" in result.stdout
    assert not (project.root / "agent-started").exists()


@pytest.mark.timeout(30)
def test_report_cannot_be_a_spec_file(project: _Project) -> None:
    spec = project.spec(PASSES, start="touch agent-started")
    before = (project.root / spec).read_text(encoding="utf-8")
    project.report = project.root / spec

    result = project.run(spec)

    assert result.returncode == 2
    assert "is a spec" in result.stdout
    assert (project.root / spec).read_text(encoding="utf-8") == before


@pytest.mark.timeout(30)
def test_i1_a_yaml_key_that_is_not_text_keeps_every_result(project: _Project) -> None:
    """YAML reads a bare `on:` as a boolean: the run and its report have to survive it."""
    spec = project.spec("function_calls: [{name: switch, params: {on: true, device: lamp}}]")

    result = project.run(spec)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    data = project.data()
    assert (data["exit_code"], data["complete"]) == (1, True)
    assert data["summary"]["failed"] == 1


def _piped(project: _Project, spec: str, reader: str) -> subprocess.CompletedProcess[str]:
    """The CLI with ``reader`` on its stdout; the code is the CLI's, not the reader's."""
    command = shlex.join(project.command(spec, "--report", f"json={project.report}"))
    return subprocess.run(
        ["bash", "-c", f"set -o pipefail; {command} | {reader}"],
        cwd=project.root,
        env=project.env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.timeout(30)
def test_i2_a_reader_that_left_after_the_summary_keeps_the_code(project: _Project) -> None:
    """`Report:` comes after the summary. Printing it to nobody cannot turn a 0 into a 1."""
    result = _piped(project, project.spec(PASSES), "sed -n '/passed/{p;q;}'")

    assert result.returncode == 0, result.stdout + result.stderr
    assert project.data()["exit_code"] == 0


@pytest.mark.timeout(30)
def test_a_reader_that_left_early_is_not_called_a_signal(project: _Project) -> None:
    result = _piped(project, project.spec(PASSES), "head -1")

    data = project.data()
    assert data["exit_code"] == result.returncode
    assert data["stopped"]["reason"] == "interrupted"
    assert "signal" not in data["stopped"]["message"]


@pytest.mark.timeout(40)
def test_i4_a_value_from_one_spec_stays_masked_in_the_next(project: _Project) -> None:
    token = "s3cr3t-token-value-0123456789"
    project.spec(PASSES, name="specs/a.highbar.yaml")
    first = project.root / "specs" / "a.highbar.yaml"
    headers = '  request:\n    headers:\n      Authorization: "Bearer ${HB_TEST_TOKEN}"\n'
    first.write_text(
        first.read_text(encoding="utf-8").replace("suites:", headers + "suites:", 1),
        encoding="utf-8",
    )
    project.spec(FAILS, name="specs/b.highbar.yaml", args=("--replies", f"token={token}"))

    result = subprocess.run(
        project.command("specs", "--report", f"json={project.report}"),
        cwd=project.root,
        env={**project.env, "HB_TEST_TOKEN": token},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert token not in project.report.read_text(encoding="utf-8")
    assert token not in result.stdout + result.stderr
    (turn,) = project.data()["specs"][1]["tests"][0]["phrasings"][0]["executions"][0]["turns"]
    assert turn["response"] == "token=${HB_TEST_TOKEN}"
