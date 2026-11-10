"""HB-34: executions in a row that fail for one cause of the infrastructure stop the run.

The count is of executions, checked after each one, across tests and specs. Anything
else an execution ends in starts it over: errors now and then are min_pass's.
"""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.adapters.http_client import AgentResponseError, InfraCause
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.cli.main import EXIT_REPEATED_ERRORS
from highbar.domain.models import (
    BUDGET_EXCEEDED,
    REPEATED_ERRORS,
    AgentConfig,
    Budget,
    Counts,
    Expect,
    ResponseExpect,
    RunSummary,
    Spec,
    Status,
    Suite,
    Trace,
    Turn,
    TurnRecord,
    Usage,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.placeholders import Values
from highbar.engine.run_spec import _run as run_to_the_end
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.session import SessionError
from highbar.engine.streak import EXIT_CODE, ErrorStreak, stop_message
from tests.unit.engine.fakes import FakeAgent

ENDPOINT = "http://127.0.0.1:1/chat"
SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))
TAIL = "; the infrastructure failed, not the agent, so the run stopped. Exits with 4."
BODY = "upstream exploded"


def _cause(kind: Any = "http_status", status: int | None = 500, **fields: Any) -> InfraCause:
    return InfraCause(kind, fields.pop("endpoint", ENDPOINT), status=status, **fields)


class _Down(FakeAgent):
    """A ``FakeAgent`` whose sends end, in turn, as ``outcomes`` say, then as the last
    one (or from the first again, with ``cycle``).

    "ok" answers "ok"; "no" answers "no", which SAYS_OK fails; "silent" answers "ok" and
    records nothing; "unreadable" is a reply that came and cannot be read; "500" and
    "503" are that status, with a body that changes every time; "timeout" and "refused"
    are those failures; "elsewhere" is a 500 from another endpoint.
    """

    def __init__(
        self,
        *outcomes: str,
        cycle: bool = False,
        endpoint: str = ENDPOINT,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(self._write)
        self._outcomes = outcomes
        self._cycle = cycle
        self._endpoint = endpoint
        self._usage = usage
        self._current = "ok"
        self.messages: list[str] = []  # every send, the failed ones too

    def _write(self, message: str, test_id: str) -> Window:
        if self._current == "silent":
            return Window()
        return Window(turns=[TurnRecord(ts=1.0, context={"test": test_id}, usage=self._usage)])

    def send(self, message: str, *, values: Values) -> str:
        sent = len(self.messages)
        self.messages.append(message)
        count = len(self._outcomes)
        self._current = self._outcomes[sent % count if self._cycle else min(sent, count - 1)]
        if self._current in ("500", "503", "elsewhere"):
            status = 503 if self._current == "503" else 500
            elsewhere = self._current == "elsewhere"
            endpoint = "http://elsewhere.test/chat" if elsewhere else self._endpoint
            raise AgentResponseError(
                f"HTTP {status} from {endpoint}: {BODY}, request {sent}",
                InfraCause("http_status", endpoint, status=status),
            )
        if self._current == "timeout":
            cause = InfraCause("timeout", self._endpoint, seconds=60)
            raise AgentResponseError("no response in 60s (agent.timeout)", cause)
        if self._current == "refused":
            cause = InfraCause("connect", self._endpoint)
            raise AgentResponseError(f"cannot reach {self._endpoint}: refused", cause)
        if self._current == "unreadable":
            raise AgentResponseError("the reply is JSON null: there is no text to check")
        reply = super().send(message, values=values)
        return "no" if self._current == "no" else reply


def _runner(
    agent: FakeAgent,
    *,
    max_errors: int | None = 5,
    streak: ErrorStreak | None = None,
    reporter: MagicMock | None = None,
    **options: Any,
) -> Runner:
    return Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=0,
        max_errors=max_errors,
        streak=streak,
        **options,
    )


def _spec(*tests: Case) -> Spec:
    return Spec(
        agent=AgentConfig(start="agent", endpoint=ENDPOINT),
        suites=[Suite(name="S", tests=list(tests))],
    )


def _run(tests: list[Case], agent: FakeAgent, **options: Any) -> RunSummary:
    return _runner(agent, **options).run(_spec(*tests))


def _test(name: str = "t", runs: int = 10, **fields: Any) -> Case:
    return Case(trigger=name, expect=SAYS_OK, runs=runs, **fields)


def _ended(tests: list[Case], agent: FakeAgent) -> tuple[RunSummary, bool]:
    """The summary, and whether the spec ran to its end, as the report is told."""
    ends: list[bool] = []
    summary = run_to_the_end(_runner(agent), _spec(*tests), lambda _, done: ends.append(done))
    return summary, ends[-1]


class TestStop:
    def test_hb34_ac1_five_errors_in_a_row_stop_the_run_at_the_fifth(self) -> None:
        agent = _Down("500")

        summary = _run([_test("refund policy"), _test("next")], agent)

        assert len(agent.messages) == 5
        assert summary.stop_reason == REPEATED_ERRORS
        assert summary.stopped == (
            f"5 executions in a row failed with HTTP 500 from {ENDPOINT} "
            f"(last: 'refund policy'){TAIL}"
        )
        assert [test.name for test in summary.tests] == ["refund policy"]

    def test_hb34_ac2_the_count_is_the_options(self) -> None:
        agent = _Down("500")

        summary = _run([_test()], agent, max_errors=3)

        assert len(agent.messages) == 3
        assert summary.stopped is not None
        assert summary.stopped.startswith("3 executions in a row failed with HTTP 500")

    @pytest.mark.parametrize("off", [None, 0])
    def test_hb34_ac3_off_every_execution_runs(self, off: int | None) -> None:
        agent = _Down("500")

        summary = _run([_test(), _test("next", runs=2)], agent, max_errors=off)

        assert len(agent.messages) == 12
        assert (summary.stopped, summary.stop_reason) == (None, None)
        first, second = summary.tests
        assert first.counts == Counts(error=10)
        assert (first.status, second.status) == (Status.ERROR, Status.ERROR)

    def test_hb34_a_count_of_one_stops_at_the_first_error(self) -> None:
        summary = _run([_test()], _Down("500"), max_errors=1)

        assert summary.stopped is not None
        assert summary.stopped.startswith(f"1 execution failed with HTTP 500 from {ENDPOINT}")

    def test_hb34_the_agent_is_stopped_and_the_suites_left_do_not_start(self) -> None:
        reporter = MagicMock()
        runner = _runner(_Down("500"), reporter=reporter)
        spec = Spec(
            agent=AgentConfig(start="agent", endpoint=ENDPOINT),
            suites=[Suite(name="A", tests=[_test(runs=5)]), Suite(name="B", tests=[_test()])],
        )

        runner.run(spec)

        runner._adapter.stop.assert_called_once()
        assert reporter.on_suite_start.call_count == 1


class TestSameCause:
    def test_hb34_ac4_bodies_that_differ_are_one_cause(self) -> None:
        agent = _Down("500")

        (test,) = _run([_test()], agent).tests

        errors = [execution.errors for execution in test.phrasings[0].executions]
        assert len(errors) == 5
        assert len(set(errors)) == 5

    def test_hb34_ac4_a_timeout_between_errors_starts_the_count_over(self) -> None:
        agent = _Down("500", "500", "timeout", "500", "500")

        summary = _run([_test(runs=5)], agent)

        assert len(agent.messages) == 5
        assert (summary.stopped, summary.stop_reason) == (None, None)

    @pytest.mark.parametrize("other", ["503", "timeout", "refused", "elsewhere"])
    def test_hb34_another_status_kind_or_endpoint_starts_it_over(self, other: str) -> None:
        agent = _Down("500", "500", "500", "500", other, "500", "500", "500", "500")

        summary = _run([_test(runs=9)], agent)

        assert len(agent.messages) == 9
        assert summary.stopped is None

    @pytest.mark.parametrize(
        ("outcome", "phrase"),
        [
            ("503", f"with HTTP 503 from {ENDPOINT}"),
            ("timeout", f"with no response from {ENDPOINT} within agent.timeout (60s)"),
            ("refused", f"to connect to {ENDPOINT}"),
            ("elsewhere", "with HTTP 500 from http://elsewhere.test/chat"),
        ],
    )
    def test_hb34_each_cause_repeated_stops_the_run_and_is_named(
        self, outcome: str, phrase: str
    ) -> None:
        summary = _run([_test()], _Down(outcome))

        assert summary.stop_reason == REPEATED_ERRORS
        assert summary.stopped == f"5 executions in a row failed {phrase} (last: 't'){TAIL}"


class TestStartOver:
    @pytest.mark.parametrize("between", ["ok", "no", "unreadable"])
    def test_hb34_ac5_any_other_ending_starts_the_count_over(self, between: str) -> None:
        agent = _Down("500", "500", "500", "500", between, "500", "500", "500", "500")

        summary = _run([_test(runs=9)], agent)

        assert len(agent.messages) == 9
        assert summary.stopped is None
        (test,) = summary.tests
        assert test.counts.error >= 8

    def test_hb34_ac5_not_observed_starts_it_over(self) -> None:
        expect = Expect(function_calls=[])
        agent = _Down("500", "500", "500", "500", "silent", "500", "500", "500", "500")

        summary = _run([Case(trigger="t", expect=expect, runs=9)], agent, managed=False)

        assert len(agent.messages) == 9
        assert summary.stopped is None
        (test,) = summary.tests
        assert any("not observed" in error for error in test.errors)

    def test_hb34_ac5_a_session_error_starts_it_over(self) -> None:
        setups = itertools.count()

        def setup(context: Any) -> dict[str, Any]:
            if next(setups) == 4:
                raise SessionError("session.setup failed: exit 1")
            return {}

        session = MagicMock()
        session.setup.side_effect = setup
        agent = _Down("500")

        summary = _run([_test(runs=9)], agent, session=session)

        # The fifth execution sent nothing: its session never opened.
        assert len(agent.messages) == 8
        assert summary.stopped is None

    def test_hb34_ac5_a_template_error_starts_it_over(self) -> None:
        broken = Case(trigger="cancel {{previous.function_calls[0].result.id}}", expect=SAYS_OK)
        agent = _Down("500")

        summary = _run([_test("a", runs=4), broken, _test("b", runs=4)], agent)

        assert len(agent.messages) == 8
        assert summary.stopped is None
        assert summary.tests[1].status is Status.ERROR

    def test_hb34_ac5_a_failure_by_a_budget_starts_it_over(self) -> None:
        """A conversation whose second turn got a 500 failed when its first spent too much."""
        turns = [Turn(trigger="a", expect=SAYS_OK), Turn(trigger="b", expect=SAYS_OK)]
        budget = Expect(budget=Budget(max_input_tokens=1))
        agent = _Down("ok", "500", cycle=True, usage=Usage(input_tokens=100))

        summary = _run([Case(name="c", turns=turns, expect=budget, runs=10)], agent)

        assert len(agent.messages) == 20
        assert summary.stopped is None
        (outcome,) = summary.tests
        assert outcome.counts == Counts(failed=10)

    def test_hb34_ac5_errors_now_and_then_never_stop_the_run(self) -> None:
        agent = _Down("500", "500", "ok", cycle=True)

        summary = _run([_test(runs=30)], agent)

        assert len(agent.messages) == 30
        assert summary.stopped is None


class TestAcross:
    def test_hb34_ac6_the_count_goes_on_into_the_next_test(self) -> None:
        agent = _Down("ok", "ok", "500")

        summary = _run([_test("a", runs=5), _test("b", runs=5), _test("c")], agent)

        assert len(agent.messages) == 7
        a, b = summary.tests
        assert a.counts == Counts(passed=2, error=3)
        assert b.counts == Counts(error=2)
        assert len(b.phrasings[0].executions) == 2
        assert summary.stopped is not None
        assert "5 executions in a row" in summary.stopped
        assert "(last: 'b')" in summary.stopped

    def test_hb34_ac6_the_count_goes_on_into_the_next_spec(self) -> None:
        streak = ErrorStreak()

        first = _run([_test("a", runs=5)], _Down("ok", "ok", "500"), streak=streak)

        assert first.stopped is None
        assert streak.count == 3
        agent = _Down("500")
        second = _run([_test("b", runs=5), _test("c")], agent, streak=streak)
        assert len(agent.messages) == 2
        assert second.stop_reason == REPEATED_ERRORS
        assert second.stopped is not None
        assert second.stopped.startswith("5 executions in a row")
        assert [test.name for test in second.tests] == ["b"]

    def test_hb34_a_conversation_counts_once_with_the_cause_of_the_turn_that_failed(self) -> None:
        turns = [Turn(trigger="hi", expect=SAYS_OK), Turn(trigger="refund", expect=SAYS_OK)]
        agent = _Down("ok", "500", cycle=True)

        summary = _run([Case(name="refund flow", turns=turns, runs=10)], agent)

        assert len(agent.messages) == 10  # two turns each, five executions
        assert summary.stopped is not None
        assert summary.stopped.startswith("5 executions in a row failed with HTTP 500")
        assert "(last: 'refund flow')" in summary.stopped

    def test_hb34_each_run_of_each_phrasing_is_an_execution(self) -> None:
        generator = MagicMock()
        generator.generate.return_value = ["v1", "v2"]
        agent = _Down("500")

        summary = _run(
            [Case(trigger="t", expect=SAYS_OK, variations=2, runs=2)], agent, variations=generator
        )

        assert len(agent.messages) == 5
        (test,) = summary.tests
        assert [len(phrasing.executions) for phrasing in test.phrasings] == [2, 2, 1]
        assert [phrasing.status for phrasing in test.phrasings] == [Status.ERROR] * 3
        assert test.status is Status.ERROR
        assert test.errors[-1] == "the run stopped before this test ended: 5 of 6 executions ran"


class TestTheTestItStoppedIn:
    def test_hb34_ac7_it_is_an_error_with_the_executions_it_had(self) -> None:
        reporter = MagicMock()

        summary = _run([_test("a"), _test("b")], _Down("500"), reporter=reporter)

        (test,) = summary.tests
        assert (summary.total, summary.errored) == (1, 1)
        assert (test.status, test.runs, test.counts) == (Status.ERROR, 10, Counts(error=5))
        (phrasing,) = test.phrasings
        assert phrasing.status is Status.ERROR
        assert [execution.run for execution in phrasing.executions] == [1, 2, 3, 4, 5]
        assert {execution.status for execution in phrasing.executions} == {Status.ERROR}
        # The terminal shows it as it shows any test, with what ran.
        name, result = reporter.on_test_result.call_args[0]
        assert name == "a"
        assert (result.status, result.counts) == (Status.ERROR, (0, 5, 10))
        stopped = "the run stopped before this test ended: 5 of 10 executions ran"
        assert result.errors[-1] == stopped
        assert test.errors[-1] == stopped

    def test_hb34_ac7_past_its_min_pass_it_is_still_an_error(self) -> None:
        """A test the run stopped in did not run all it had to: it is no pass."""
        agent = _Down("ok", "ok", "ok", "500")

        summary = _run([_test("a", min_pass=3)], agent)

        (test,) = summary.tests
        assert test.counts == Counts(passed=3, error=5)
        assert test.status is Status.ERROR
        assert test.phrasings[0].status is Status.ERROR
        assert (summary.passed, summary.errored) == (0, 1)

    def test_hb34_ac7_the_spec_it_stopped_in_is_not_complete(self) -> None:
        summary, complete = _ended([_test("a")], _Down("500"))

        assert summary.total == 1
        assert complete is False

    def test_hb34_ac8_a_stop_at_the_last_execution_of_the_last_test_leaves_it_complete(
        self,
    ) -> None:
        summary, complete = _ended([_test("a", runs=5)], _Down("500"))

        assert summary.stop_reason == REPEATED_ERRORS
        assert complete is True
        (test,) = summary.tests
        assert test.status is Status.ERROR
        assert not any("stopped before" in error for error in test.errors)

    def test_hb34_a_stop_at_the_end_of_a_test_before_the_last_leaves_the_rest_out(self) -> None:
        summary, complete = _ended([_test("a", runs=5), _test("b")], _Down("500"))

        assert [test.name for test in summary.tests] == ["a"]
        assert complete is False

    def test_hb34_a_late_call_still_fails_a_test_before_the_stop(self) -> None:
        """The read after the last test still happens: what was late still counts."""

        class _LateRefund(_Down):
            """Its first message records a call to refund after the test's window."""

            def _write(self, message: str, test_id: str) -> Any:
                window = super()._write(message, test_id)
                if len(self.messages) > 1:
                    return window
                refund = Trace(
                    fn="refund",
                    args={},
                    result=None,
                    started_at=1.0,
                    ended_at=1.0,
                    context={"test": test_id},
                )
                return window, Window(tools=[refund])

        guard = Case(trigger="guard", expect=Expect(forbid=("refund",)))

        summary = _run([guard, _test("b")], _LateRefund("ok", "500"))

        assert summary.stop_reason == REPEATED_ERRORS
        guarded, stopped_in = summary.tests
        assert guarded.status is Status.FAILED
        assert stopped_in.status is Status.ERROR


class TestTwoStops:
    def test_hb34_ac10_a_ceiling_still_stops_as_budget_exceeded(self) -> None:
        agent = _Down("ok", usage=Usage(cost=1.0, currency="USD"))

        summary = _run([_test("a", runs=1), _test("b", runs=1)], agent, max_cost=0.5)

        assert summary.stop_reason == BUDGET_EXCEEDED
        assert summary.stopped is not None
        assert summary.stopped.startswith("--max-cost 0.5")

    def test_hb34_the_stop_that_came_first_is_the_reason(self) -> None:
        """The errors stop the test the ceiling would have stopped at its end."""
        agent = _Down("ok", "500", usage=Usage(cost=1.0, currency="USD"))

        summary = _run([_test("a", runs=7), _test("b")], agent, max_cost=0.5)

        assert len(agent.messages) == 6
        assert summary.stop_reason == REPEATED_ERRORS


class TestTheMessage:
    def test_hb34_ac9_a_value_from_var_in_the_endpoint_is_masked_and_no_body_shows(self) -> None:
        secret = "s3cr3t-key-value"
        reporter = MagicMock()
        agent = _Down("500", endpoint=f"http://agent.test/chat?key={secret}")

        summary = _run([_test()], agent, secrets={secret: "AGENT_KEY"}, reporter=reporter)

        assert summary.stopped is not None
        assert secret not in summary.stopped
        assert "from http://agent.test/chat?key=${AGENT_KEY} (last: 't')" in summary.stopped
        assert BODY not in summary.stopped
        assert secret not in str(reporter.mock_calls)
        assert secret not in str(summary.tests)

    def test_hb34_the_message_states_the_code_the_cli_exits_with(self) -> None:
        assert EXIT_CODE == EXIT_REPEATED_ERRORS == 4
        assert stop_message(5, _cause(), "t").endswith(f"Exits with {EXIT_REPEATED_ERRORS}.")

    @pytest.mark.parametrize(
        ("cause", "phrase"),
        [
            (_cause(), f"with HTTP 500 from {ENDPOINT}"),
            (_cause("connect", None), f"to connect to {ENDPOINT}"),
            (_cause("transport", None), f"with a transport error from {ENDPOINT}"),
            (_cause("empty_body", None), f"with an empty body from {ENDPOINT}"),
            (_cause("timeout", None), f"with no response from {ENDPOINT} within agent.timeout"),
        ],
    )
    def test_hb34_it_names_the_count_the_cause_the_endpoint_and_the_test(
        self, cause: InfraCause, phrase: str
    ) -> None:
        message = stop_message(5, cause, "refund policy")

        assert message == f"5 executions in a row failed {phrase} (last: 'refund policy'){TAIL}"


class TestStreak:
    def test_hb34_the_same_cause_adds_another_starts_at_one_anything_else_at_zero(self) -> None:
        streak = ErrorStreak()

        assert [streak.add(_cause()), streak.add(_cause())] == [1, 2]
        assert streak.add(_cause(status=503)) == 1
        assert streak.add(_cause(status=503, endpoint="http://elsewhere.test/chat")) == 1
        assert streak.add(None) == 0
        assert streak.cause is None
