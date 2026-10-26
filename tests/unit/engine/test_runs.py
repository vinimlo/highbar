"""runs and min_pass (D9): a flaky test becomes a gate instead of a coin toss."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Expect,
    FunctionCallExpect,
    Spec,
    Status,
    Suite,
    Trace,
    TurnRecord,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.domain.models import TestResult as Result
from highbar.engine.parser import parse_spec
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.runs import check_runs, phrasing_status, resolve_runs
from highbar.engine.schema import SpecError
from highbar.reporters.terminal import TerminalReporter
from tests.unit.engine.fakes import FakeAgent

CALLS_LOOKUP = Expect(function_calls=[FunctionCallExpect(name="lookup")])


def _reporter(out: StringIO) -> TerminalReporter:
    reporter = TerminalReporter()
    reporter._console = Console(file=out, width=200, color_system=None)
    return reporter


def _result(status: Status) -> Result:
    return Result(status=status, errors=[], duration_ms=1.0)


def _flaky(passes: list[bool]):
    """An agent that calls `lookup` on the runs marked True, in order."""
    outcomes = iter(passes)

    def script(message: str, test_id: str) -> Window:
        tools = [Trace(fn="lookup", args={}, result=None, started_at=1.0, ended_at=1.1,
                       context={"test": test_id})] if next(outcomes) else []  # fmt: skip
        return Window(tools=tools, turns=[TurnRecord(ts=1.0, context={"test": test_id})])

    return script


def _run(
    tests: list[Case], agent: FakeAgent, *, default_runs: int = 1, variations: object = None
) -> tuple[list[tuple[str, Result]], MagicMock]:
    reporter = MagicMock()
    Runner(
        adapter=MagicMock(),
        client=agent,
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter,
        settle_time=0,
        variations=variations,  # type: ignore[arg-type]
        default_runs=default_runs,
    ).run(
        Spec(
            agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
            suites=[Suite(name="S", tests=tests)],
        )
    )
    return [c[0] for c in reporter.on_test_result.call_args_list], reporter


class TestResolve:
    def test_fraction_min_pass_rounds_up(self) -> None:
        assert resolve_runs(Case(trigger="a", runs=5, min_pass=0.8), 1) == (5, 4)
        assert resolve_runs(Case(trigger="a", runs=3, min_pass=0.5), 1) == (3, 2)
        # 0.7 * 10 is 7.000000000000001 in binary floating point: still 7.
        assert resolve_runs(Case(trigger="a", runs=10, min_pass=0.7), 1) == (10, 7)

    def test_min_pass_defaults_to_every_run(self) -> None:
        assert resolve_runs(Case(trigger="a", runs=5), 1) == (5, 5)

    def test_cli_runs_overrides_the_spec_default_not_the_test(self) -> None:
        # The CLI's --runs arrives as default_runs, in place of defaults.runs.
        assert resolve_runs(Case(trigger="a"), 5) == (5, 5)
        assert resolve_runs(Case(trigger="a", runs=2), 5) == (2, 2)

    def test_min_pass_above_runs_names_the_test(self) -> None:
        spec = Spec(
            agent=AgentConfig(start="a", endpoint="http://127.0.0.1:1/chat"),
            suites=[Suite(name="S", tests=[Case(trigger="a"), Case(trigger="b", min_pass=4)])],
        )

        check_runs(spec, 5)
        with pytest.raises(SpecError, match=r"suites\[0\]\.tests\[1\].*min_pass 4.*3 runs"):
            check_runs(spec, 3)


class TestPhrasingStatus:
    def test_passes_at_min_pass(self) -> None:
        statuses = [Status.PASSED] * 3 + [Status.FAILED] * 2

        assert phrasing_status(statuses, 3) is Status.PASSED
        assert phrasing_status(statuses, 4) is Status.FAILED

    def test_errored_run_counts_as_not_passed(self) -> None:
        statuses = [Status.PASSED] * 2 + [Status.ERROR] + [Status.FAILED] * 2

        assert phrasing_status(statuses, 4) is Status.FAILED

    def test_i5_runs_that_could_not_be_checked_are_no_evidence_of_failure(self) -> None:
        """3 passed, 2 timed out, min 4: nothing shows the agent did wrong (D2)."""
        statuses = [Status.PASSED] * 3 + [Status.ERROR] * 2

        assert phrasing_status(statuses, 4) is Status.ERROR

    def test_all_errored_runs_are_an_error_not_a_failure(self) -> None:
        assert phrasing_status([Status.ERROR] * 3, 2) is Status.ERROR


class TestRunnerRuns:
    def test_runs_pass_when_min_pass_is_reached(self) -> None:
        agent = FakeAgent(_flaky([True, False, True, False, True]))

        ((_, result),), _ = _run(
            [Case(trigger="a", expect=CALLS_LOOKUP, runs=5, min_pass=3)], agent
        )

        assert result.status is Status.PASSED
        assert result.counts == (3, 5, 3)
        assert len(agent.sent) == 5

    def test_runs_fail_below_min_pass_and_show_the_count(self) -> None:
        agent = FakeAgent(_flaky([True, False, True, False, True]))

        ((name, result),), _ = _run(
            [Case(trigger="cancel my order", expect=CALLS_LOOKUP, runs=5, min_pass=4)], agent
        )

        assert result.status is Status.FAILED
        # The same failure in two runs is one line that says how often.
        assert result.errors == [
            "Expected call to 'lookup', but the agent called: nothing (2 of 5 runs)"
        ]
        out = StringIO()
        _reporter(out).on_test_result(name, result)
        assert "cancel my order 3/5 (min 4)" in out.getvalue()

    def test_each_run_is_its_own_execution(self) -> None:
        agent = FakeAgent()
        hooks = MagicMock()
        hooks.setup.return_value = {}

        reporter = MagicMock()
        Runner(
            adapter=MagicMock(),
            client=agent,
            collector=agent,
            assertions=[FunctionCallsAssertion()],
            reporter=reporter,
            settle_time=0,
            session=hooks,
            default_runs=3,
        ).run(
            Spec(
                agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
                suites=[
                    Suite(name="S", tests=[Case(trigger="a", expect=Expect(function_calls=[]))])
                ],
            )
        )

        assert len(set(agent.test_ids)) == 3
        assert hooks.setup.call_count == 3
        assert [c.args[0].test_id for c in hooks.setup.call_args_list] == agent.test_ids

    def test_each_phrasing_must_reach_min_pass(self) -> None:
        generator = MagicMock()
        generator.generate.return_value = ["b"]
        # Trigger "a": 2 of 2 runs call lookup. Variation "b": 1 of 2.
        agent = FakeAgent(_flaky([True, True, True, False]))

        ((_, result),), _ = _run(
            [Case(trigger="a", expect=CALLS_LOOKUP, variations=1, runs=2, min_pass=2)],
            agent,
            variations=generator,
        )

        assert result.status is Status.FAILED
        assert result.counts is None
        assert '[variation: "b"] 1/2 runs passed (min 2)' in result.errors

    def test_zero_variations_available_is_an_error(self) -> None:
        generator = MagicMock()
        generator.generate.return_value = []
        agent = FakeAgent()

        ((_, result),), _ = _run(
            [Case(trigger="a", expect=CALLS_LOOKUP, variations=3)], agent, variations=generator
        )

        assert result.status is Status.ERROR
        assert result.errors == ["no variations available (0 of 3)"]
        assert agent.sent == []

    def test_runs_1_output_is_unchanged(self) -> None:
        agent = FakeAgent(_flaky([False]))

        ((name, result),), _ = _run([Case(trigger="a", expect=CALLS_LOOKUP)], agent)

        assert result.counts is None
        assert result.errors == ["Expected call to 'lookup', but the agent called: nothing"]
        out = StringIO()
        _reporter(out).on_test_result(name, result)
        assert "/1" not in out.getvalue()


AGENT = """\
agent:
  start: "python agent.py"
  endpoint: "http://localhost:8000/chat"
"""


def _spec(tmp_path: Path, body: str) -> str:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(AGENT + textwrap.dedent(body), encoding="utf-8")
    return str(path)


class TestSpec:
    def test_defaults_and_test_level_runs_are_parsed(self, tmp_path: Path) -> None:
        spec = parse_spec(
            _spec(
                tmp_path,
                """\
                defaults: {runs: 3}
                suites:
                  - name: S
                    tests:
                      - {trigger: "a", expect: {function_calls: []}, runs: 5, min_pass: 0.8}
                """,
            )
        )

        (test,) = spec.suites[0].tests
        assert spec.default_runs == 3
        assert (test.runs, test.min_pass) == (5, 0.8)

    @pytest.mark.parametrize(
        "fields, message",
        [
            ("runs: 0", "runs must be a whole number of at least 1"),
            ("runs: true", "runs must be a whole number of at least 1"),
            ("min_pass: 0", "min_pass must be a whole number of at least 1 or a fraction"),
            ("min_pass: 1.5", "min_pass must be a whole number of at least 1 or a fraction"),
        ],
    )
    def test_runs_and_min_pass_values(self, tmp_path: Path, fields: str, message: str) -> None:
        path = _spec(
            tmp_path,
            f"""\
            suites:
              - name: S
                tests:
                  - {{trigger: "a", expect: {{function_calls: []}}, {fields}}}
            """,
        )

        with pytest.raises(SpecError, match=message):
            parse_spec(path)

    def test_i3_cli_runs_lifts_an_integer_min_pass(self, tmp_path: Path) -> None:
        """A spec written for CI: min_pass 3, no runs, run with --runs 5."""
        (tmp_path / "spec.highbar.yaml").write_text(
            f"""\
agent:
  start: "{sys.executable} -c \\"import sys; sys.exit(1)\\""
  endpoint: "http://127.0.0.1:9/chat"
suites:
  - name: S
    tests:
      - {{trigger: "a", expect: {{function_calls: []}}, min_pass: 3}}
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, "-m", "highbar.cli.main", "run", "spec.highbar.yaml", "--runs", "5"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert "min_pass" not in result.stdout
        assert "could not start the agent" in result.stdout

    def test_min_pass_above_runs_fails_validation(self, tmp_path: Path) -> None:
        path = _spec(
            tmp_path,
            """\
            defaults: {runs: 3}
            suites:
              - name: S
                tests:
                  - {trigger: "a", expect: {function_calls: []}, min_pass: 4}
            """,
        )

        parse_spec(path)  # --runs could still make it possible
        result = subprocess.run(
            [sys.executable, "-m", "highbar.cli.main", "validate", path],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 2, result.stdout + result.stderr
        assert "min_pass 4 is more than its 3 runs" in result.stdout

    def test_min_pass_above_cli_runs_exits_2_before_any_agent(self, tmp_path: Path) -> None:
        marker = tmp_path / "STARTED"
        (tmp_path / "spec.highbar.yaml").write_text(
            f"""\
agent:
  start: "{sys.executable} -c \\"open('STARTED', 'w')\\""
  endpoint: "http://127.0.0.1:9/chat"
defaults: {{runs: 5}}
suites:
  - name: S
    tests:
      - {{trigger: "a", expect: {{function_calls: []}}, min_pass: 4}}
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, "-m", "highbar.cli.main", "run", "spec.highbar.yaml", "--runs", "3"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 2, result.stdout + result.stderr
        assert "min_pass 4 is more than its 3 runs" in result.stdout
        assert not marker.exists()


def _late_delete(on_runs: set[int]):
    """Every run calls lookup; the runs in `on_runs` (1-based) also delete, late."""
    count = 0

    def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
        nonlocal count
        count += 1
        lookup = Trace(fn="lookup", args={}, result=None, started_at=1.0, ended_at=1.1,
                       context={"test": test_id})  # fmt: skip
        now = Window(tools=[lookup], turns=[TurnRecord(ts=1.0, context={"test": test_id})])
        if count not in on_runs:
            return now
        delete = Trace(fn="delete_account", args={}, result=None, started_at=2.0,
                       ended_at=2.1, context={"test": test_id})  # fmt: skip
        return now, Window(tools=[delete])

    return script


NEVER_DELETES = Expect(
    function_calls=[FunctionCallExpect(name="lookup")], forbid=("delete_account",)
)


class TestLateCallsAndMinPass:
    """PR 6 review, I1: a late call fails its own run; the test is judged again."""

    def test_late_call_fails_its_run_and_min_pass_still_holds(self) -> None:
        ((name, result),), reporter = _run(
            [Case(trigger="where is my order", expect=NEVER_DELETES, runs=5, min_pass=4)],
            FakeAgent(_late_delete({1})),
        )

        assert result.status is Status.PASSED
        assert result.counts == (4, 5, 4)
        assert any("delete_account" in e and "(run 1 of 5)" in e for e in result.errors)
        assert reporter.on_run_end.call_args[0][0].passed == 1

    def test_late_call_that_breaks_min_pass_fails_the_test(self) -> None:
        ((_, result),), _ = _run(
            [Case(trigger="a", expect=NEVER_DELETES, runs=5, min_pass=5)],
            FakeAgent(_late_delete({1})),
        )

        assert result.status is Status.FAILED
        assert result.counts == (4, 5, 5)

    def test_late_call_on_a_reported_test_that_still_passes_is_a_warning(self) -> None:
        """The last run's late call lands while the next test runs."""
        results, reporter = _run(
            [
                Case(trigger="first", expect=NEVER_DELETES, runs=3, min_pass=2),
                Case(trigger="second", expect=CALLS_LOOKUP),
            ],
            # Declares its tools, as the SDK does: the warning under test is the only one.
            FakeAgent(_late_delete({3}), declares=("delete_account", "lookup")),
        )

        summary = reporter.on_run_end.call_args[0][0]
        assert (summary.passed, summary.failed) == (2, 0)
        (warning,) = [c.args[0] for c in reporter.on_warning.call_args_list]
        assert warning.startswith("first: a run now fails (2/3, min 2), and the test still passes")
        assert "delete_account" in warning

    def test_late_call_on_a_reported_test_below_min_pass_recounts_it(self) -> None:
        _, reporter = _run(
            [
                Case(trigger="first", expect=NEVER_DELETES, runs=3, min_pass=3),
                Case(trigger="second", expect=CALLS_LOOKUP),
            ],
            # Declares its tools, as the SDK does: the warning under test is the only one.
            FakeAgent(_late_delete({3}), declares=("delete_account", "lookup")),
        )

        summary = reporter.on_run_end.call_args[0][0]
        assert (summary.passed, summary.failed) == (1, 1)
        (warning,) = [c.args[0] for c in reporter.on_warning.call_args_list]
        assert "now fails (2/3, min 3)" in warning
