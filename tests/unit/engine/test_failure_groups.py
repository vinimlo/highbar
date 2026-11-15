"""HB-27: a phrasing's failures, one line per condition, however the reply changes.

A response check puts the agent's reply in its message, and an LLM's reply changes on
every run: merging only identical text, `runs: 5` with two failed matchers gave ten
lines. A condition is now one line with the number of runs it failed in, and the reply
of the first of them, since what the agent said is the first thing anyone asks. Each
execution keeps its own message in the report.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    Expect,
    FunctionCallExpect,
    ResponseExpect,
    Status,
    Trace,
    Turn,
    TurnRecord,
    Window,
)
from highbar.domain.models import Test as Case
from tests.unit.engine.test_outcomes import Replying, _run


def _says(**matcher: str) -> Expect:
    return Expect(response=ResponseExpect(matcher=matcher))


ORDER = _says(matches="^Order", not_matches="sorry")


class TestCondition:
    def test_hb27_a_failure_says_its_condition_apart_from_the_reply(self) -> None:
        results = ResponseAssertion().evaluate([], "sorry, no", ORDER)

        assert [(r.message, r.condition) for r in results] == [
            (
                "Response does not match pattern '^Order'. Got: 'sorry, no'",
                "Response does not match pattern '^Order'",
            ),
            (
                "Response matches forbidden pattern 'sorry' (found 'sorry'). Got: 'sorry, no'",
                "Response matches forbidden pattern 'sorry'",
            ),
        ]

    def test_hb27_a_pass_has_none(self) -> None:
        (result,) = ResponseAssertion().evaluate([], "ok", _says(contains="ok"))

        assert result.passed and result.condition is None


class TestGroups:
    def test_hb27_two_conditions_in_five_runs_are_two_lines(self) -> None:
        """The card's measurement: runs: 5 with two failed matchers gave ten lines."""
        reporter = MagicMock()
        replies = [f"sorry, try again ({n})" for n in range(1, 6)]

        summary = _run(
            [Case(trigger="place my order", expect=ORDER, runs=5)],
            Replying(list(replies)),
            reporter=reporter,
        )

        lines = [
            "Response does not match pattern '^Order'. Got: 'sorry, try again (1)' "
            "(5 of 5 runs; as in run 1)",
            "Response matches forbidden pattern 'sorry' (found 'sorry'). "
            "Got: 'sorry, try again (1)' (5 of 5 runs; as in run 1)",
        ]
        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert list(test.errors) == lines
        assert reporter.on_test_result.call_args[0][1].errors == lines

    def test_hb27_the_count_is_of_the_runs_that_failed_it(self) -> None:
        summary = _run(
            [Case(trigger="place my order", expect=_says(not_contains="sorry"), runs=3)],
            Replying(["Order placed", "sorry, no stock", "sorry, closed"]),
        )

        assert list(summary.tests[0].errors) == [
            "Response contains forbidden text 'sorry'. Got: 'sorry, no stock' "
            "(2 of 3 runs; as in run 2)"
        ]

    def test_hb27_the_same_text_keeps_its_form(self) -> None:
        summary = _run(
            [Case(trigger="hi", expect=_says(contains="ok"), runs=2)],
            Replying(["no", "no"]),
        )

        assert list(summary.tests[0].errors) == [
            "Response does not contain 'ok'. Got: 'no' (2 of 2 runs)"
        ]

    def test_hb27_two_conditions_never_share_a_line(self) -> None:
        summary = _run(
            [Case(trigger="hi", expect=_says(contains="ok", icontains="thanks"), runs=2)],
            Replying(["no 1", "no 2"]),
        )

        assert list(summary.tests[0].errors) == [
            "Response does not contain 'ok'. Got: 'no 1' (2 of 2 runs; as in run 1)",
            "Response does not contain 'thanks' (ignoring case). Got: 'no 1' "
            "(2 of 2 runs; as in run 1)",
        ]

    def test_hb27_each_execution_keeps_its_own_message(self) -> None:
        """The report's executions are what each run said, in full."""
        summary = _run(
            [Case(trigger="hi", expect=_says(contains="ok"), runs=3)],
            Replying(["no 1", "no 2", "no 3"]),
        )

        (phrasing,) = summary.tests[0].phrasings
        assert [e.errors for e in phrasing.executions] == [
            (f"Response does not contain 'ok'. Got: 'no {n}'",) for n in (1, 2, 3)
        ]

    def test_hb27_a_turn_of_a_conversation_groups_under_its_label(self) -> None:
        turns = [
            Turn(trigger="hello", expect=Expect()),
            Turn(trigger="place my order", expect=_says(contains="placed")),
        ]

        summary = _run(
            [Case(turns=turns, runs=2)],
            Replying(["hi", "sorry 1", "hi", "sorry 2"]),
        )

        assert list(summary.tests[0].errors) == [
            "turn 2/2 (place my order): Response does not contain 'placed'. "
            "Got: 'sorry 1' (2 of 2 runs; as in run 1)"
        ]


def _call(fn: str, start: float, **fields: object) -> Trace:
    return Trace(
        fn=fn,
        args=dict(fields.get("args", {})),  # type: ignore[call-overload]
        result=fields.get("result"),
        error=fields.get("error"),  # type: ignore[arg-type]
        started_at=start,
        ended_at=start + 0.1,
    )


def _conditions(traces: list[Trace], expect: Expect) -> list[tuple[str, str | None]]:
    failed = FunctionCallsAssertion().evaluate(traces, "", expect)
    return [(r.message, r.condition) for r in failed if not r.passed]


class TestCallConditions:
    """HB-27 v1: a call check's message names what the agent did, which changes per run."""

    def test_hb27_a_guardrail(self) -> None:
        got = _conditions([_call("search", 1)], Expect(function_calls=[]))

        assert got == [
            ("Expected no function calls, but got: search", "Expected no function calls")
        ]

    def test_hb27_a_call_never_made(self) -> None:
        expect = Expect(function_calls=[FunctionCallExpect(name="lookup")])

        got = _conditions([_call("search", 1)], expect)

        assert got == [
            ("Expected call to 'lookup', but the agent called: search", "Expected call to 'lookup'")
        ]

    def test_hb27_a_call_that_failed(self) -> None:
        expect = Expect(function_calls=[FunctionCallExpect(name="lookup")])

        got = _conditions([_call("lookup", 1, error="TimeoutError: slow")], expect)

        assert got == [
            ("'lookup' was called but failed: TimeoutError: slow", "'lookup' was called but failed")
        ]

    def test_hb27_a_call_with_other_params(self) -> None:
        expect = Expect(function_calls=[FunctionCallExpect(name="lookup", params={"id": 1})])

        got = _conditions([_call("lookup", 1, args={"id": 2})], expect)

        assert got == [
            (
                "'lookup' was called but didn't match: params: expected {'id': 1}, got {'id': 2}",
                "'lookup' was called but didn't match: params: expected {'id': 1}",
            )
        ]

    def test_hb27_too_few_calls_that_fit(self) -> None:
        two = [FunctionCallExpect(name="search"), FunctionCallExpect(name="search")]

        got = _conditions([_call("search", 1)], Expect(function_calls=two))

        assert got == [
            ("expected 2 calls to 'search', got 1 that fit", "expected 2 calls to 'search'")
        ]

    def test_hb27_calls_out_of_order(self) -> None:
        expect = Expect(
            function_calls=[FunctionCallExpect(name="a"), FunctionCallExpect(name="b")],
            ordered=True,
        )

        got = _conditions([_call("b", 1), _call("a", 2)], expect)

        assert got == [
            (
                "calls matched but out of order: expected a, b; got b, a",
                "calls matched but out of order: expected a, b",
            )
        ]

    def test_hb27_unexpected_calls(self) -> None:
        expect = Expect(function_calls=[FunctionCallExpect(name="a")], exclusive=True)

        got = _conditions([_call("a", 1), _call("c", 2)], expect)

        assert got == [("unexpected calls: c", "unexpected calls")]

    def test_hb27_a_forbidden_tool_says_it_all(self) -> None:
        got = _conditions([_call("delete_account", 1)], Expect(forbid=["delete_account"]))

        assert got == [("forbidden tool 'delete_account' was called", None)]


class TestCallGroups:
    def test_hb27_a_guardrail_broken_with_other_calls_per_run_is_one_line(self) -> None:
        names = iter(["search", "lookup", "search"])

        def script(message: str, test_id: str) -> Window:
            return Window(
                tools=[
                    Trace(
                        fn=next(names),
                        args={},
                        result=None,
                        started_at=1.0,
                        ended_at=1.1,
                        context={"test": test_id},
                    )
                ],
                turns=[TurnRecord(ts=1.0, context={"test": test_id})],
            )

        summary = _run(
            [Case(trigger="tell me a joke", expect=Expect(function_calls=[]), runs=3)],
            Replying(["ha", "ha", "ha"], script),
        )

        assert list(summary.tests[0].errors) == [
            "Expected no function calls, but got: search (3 of 3 runs; as in run 1)"
        ]
