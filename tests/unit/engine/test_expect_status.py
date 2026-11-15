"""HB-21: ``expect_status``, a negative control that is green when it ends as it should.

The test passes when it ends exactly in the state it expects; it fails when it passes
(a strict xpass) or ends in the other state. What happened stays in its phrasings and
executions: only the test's own status is judged against what it expects.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from highbar.domain.models import Expect, ResponseExpect, Status, Trace, Window
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError
from tests.unit.engine.fakes import FakeAgent
from tests.unit.engine.test_outcomes import Replying, _run, _turn

GUARDRAIL = Expect(function_calls=[])
SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))


def _late_delete(message: str, test_id: str) -> tuple[Window, Window]:
    """The reply comes with no call; ``delete_account`` is recorded after the window."""
    late = Trace(
        fn="delete_account",
        args={},
        result="raw",
        started_at=1.0,
        ended_at=1.25,
        context={"test": test_id},
    )
    return Window(turns=[_turn(test=test_id)]), Window(tools=[late])


class TestJudgedAgainstWhatItExpects:
    def test_hb21_a_control_that_fails_as_expected_passes(self) -> None:
        reporter = MagicMock()

        summary = _run(
            [Case(trigger="t", expect=SAYS_OK, expect_status=Status.FAILED)],
            Replying(["no"]),
            reporter=reporter,
        )

        (test,) = summary.tests
        assert (test.status, test.expect_status) == (Status.PASSED, Status.FAILED)
        # What happened is kept: the phrasing and its execution failed.
        assert test.phrasings[0].status is Status.FAILED
        assert test.phrasings[0].executions[0].status is Status.FAILED
        assert (summary.passed, summary.failed, summary.errored) == (1, 0, 0)
        shown = reporter.on_test_result.call_args[0][1]
        assert (shown.status, shown.expected) == (Status.PASSED, Status.FAILED)

    def test_hb21_a_control_that_passes_fails(self) -> None:
        summary = _run(
            [Case(trigger="t", expect=SAYS_OK, expect_status=Status.FAILED)], Replying(["ok"])
        )

        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert test.errors[0] == (
            "passed, but expect_status is failed: this negative control no longer "
            "catches what it was written for"
        )
        assert (summary.passed, summary.failed) == (0, 1)

    def test_hb21_a_control_that_ends_in_the_other_state_fails(self) -> None:
        summary = _run(
            [Case(trigger="t", expect=SAYS_OK, expect_status=Status.FAILED)],
            Replying([OSError("connection reset")]),
        )

        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert test.errors[0] == "ended error, but expect_status is failed"
        assert any("connection reset" in error for error in test.errors[1:])

    def test_hb21_expect_status_error_passes_on_an_unverified_test(self) -> None:
        summary = _run(
            [Case(trigger="t", expect=SAYS_OK, expect_status=Status.ERROR)],
            Replying([OSError("connection reset")]),
        )

        assert summary.tests[0].status is Status.PASSED
        assert summary.errored == 0

    def test_hb21_expect_status_error_fails_on_a_failed_test(self) -> None:
        summary = _run(
            [Case(trigger="t", expect=SAYS_OK, expect_status=Status.ERROR)], Replying(["no"])
        )

        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert test.errors[0] == "ended failed, but expect_status is error"

    def test_hb21_a_test_without_it_is_judged_as_before(self) -> None:
        summary = _run([Case(trigger="t", expect=SAYS_OK)], Replying(["no"]))

        (test,) = summary.tests
        assert (test.status, test.expect_status) == (Status.FAILED, None)


class TestLateCalls:
    """A late call changes what happened, and the test is judged again (D13)."""

    def test_hb21_a_late_call_that_fails_a_control_makes_it_pass(self) -> None:
        reporter = MagicMock()

        summary = _run(
            [Case(trigger="close my account", expect=GUARDRAIL, expect_status=Status.FAILED)],
            FakeAgent(_late_delete),
            reporter=reporter,
        )

        # When it was shown, no call had been seen: the control had not failed yet.
        assert reporter.on_test_result.call_args[0][1].status is Status.FAILED
        (test,) = summary.tests
        assert test.status is Status.PASSED
        assert not any("expect_status" in error for error in test.errors)
        assert any("delete_account" in error for error in test.errors)
        assert any("now passes" in warning for warning in summary.warnings)

    def test_hb21_a_late_call_that_fails_a_test_expecting_error_fails_it(self) -> None:
        summary = _run(
            [Case(trigger="close my account", expect=GUARDRAIL, expect_status=Status.ERROR)],
            FakeAgent(_late_delete),
        )

        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert test.errors[0] == "ended failed, but expect_status is error"


class TestDefinition:
    def test_hb21_it_is_part_of_what_the_test_measures(self) -> None:
        plain = Case(trigger="t", expect=SAYS_OK)
        control = Case(trigger="t", expect=SAYS_OK, expect_status=Status.FAILED)

        assert control.definition_sha256 != plain.definition_sha256
        # A test without it keeps the hash it had on develop: results before HB-21 still join.
        assert plain.definition_sha256 == (
            "13d2833054b12753c2eafd0170299b15502d4b19778649fd7e49ddedd4fbb54a"
        )


AGENT = 'agent:\n  start: "python agent.py"\n  endpoint: "http://localhost:8000/chat"\n'


def _parsed(tmp_path: Path, test: str) -> Case:
    path = tmp_path / "spec.highbar.yaml"
    tests = textwrap.indent(textwrap.dedent(test), "      ")
    path.write_text(AGENT + "suites:\n  - name: S\n    tests:\n" + tests, encoding="utf-8")
    return parse_spec(str(path)).suites[0].tests[0]


class TestSpec:
    @pytest.mark.parametrize("status", [Status.FAILED, Status.ERROR])
    def test_hb21_a_test_says_which_state_it_must_end_in(
        self, tmp_path: Path, status: Status
    ) -> None:
        test = _parsed(
            tmp_path,
            f"""\
            - trigger: "close my account"
              expect: {{function_calls: []}}
              expect_status: {status}
            """,
        )

        assert test.expect_status is status

    def test_hb21_a_multi_turn_test_too(self, tmp_path: Path) -> None:
        test = _parsed(
            tmp_path,
            """\
            - turns:
                - trigger: "hi"
                  expect: {function_calls: []}
              expect_status: failed
            """,
        )

        assert test.expect_status is Status.FAILED

    def test_hb21_passed_is_what_every_test_expects(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"expect_status: passed is what every test expects"):
            _parsed(
                tmp_path,
                """\
                - trigger: "hi"
                  expect: {function_calls: []}
                  expect_status: passed
                """,
            )

    @pytest.mark.parametrize("value", ["fail", "true", "[failed]"])
    def test_hb21_anything_else_is_a_spec_error(self, tmp_path: Path, value: str) -> None:
        with pytest.raises(SpecError, match=r"expect_status must be failed or error"):
            _parsed(
                tmp_path,
                f"""\
                - trigger: "hi"
                  expect: {{function_calls: []}}
                  expect_status: {value}
                """,
            )
