"""What a turn reports, added up, and held against what the spec and the run allow."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.adapters.file_collector import FileTraceCollector, TraceFormatError
from highbar.adapters.http_client import AgentResponseError
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Budget,
    Counts,
    Expect,
    FunctionCallExpect,
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
from highbar.engine.observation import AgentNotObservedError
from highbar.engine.placeholders import Values
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.session import SessionError
from tests.unit.engine.fakes import FakeAgent

SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))
FAILS = Expect(response=ResponseExpect(matcher={"contains": "absent"}))
PLAIN = Case(trigger="t", expect=SAYS_OK)
TOKEN = "s3cr3t-token-value"


def _usd(cost: float) -> Usage:
    return Usage(input_tokens=100, output_tokens=10, cost=cost, currency="USD")


def _tokens(count: int, cached: int | None = None) -> Usage:
    return Usage(input_tokens=count, output_tokens=1, cached_input_tokens=cached)


def _call(fn: str, test_id: str) -> Trace:
    context = {"test": test_id}
    return Trace(fn=fn, args={}, result=None, started_at=1.0, ended_at=1.0, context=context)


def _reporting(*usages: Usage | None) -> FakeAgent:
    """An agent whose turns report ``usages`` in order, and the last one from then on."""
    sent = itertools.count()

    def script(message: str, test_id: str) -> Window:
        usage = usages[min(next(sent), len(usages) - 1)]
        return Window(turns=[TurnRecord(ts=1.0, context={"test": test_id}, usage=usage)])

    return FakeAgent(script)


def _spending(*records: tuple[Usage | None, ...], stray: Usage | None = None) -> FakeAgent:
    """An agent whose messages each write ``records`` in turn, and an untagged ``stray``."""
    sent = itertools.count()

    def script(message: str, test_id: str) -> Window:
        written = records[min(next(sent), len(records) - 1)]
        turns = [TurnRecord(ts=1.0, context={"test": test_id}, usage=usage) for usage in written]
        return Window(turns=[*turns, *([TurnRecord(ts=1.0, usage=stray)] if stray else [])])

    return FakeAgent(script)


def _late(on_time: Usage | None, late: Usage | None) -> Callable[[str, str], Any]:
    """A script: the first message reports ``on_time``, and ``late`` after its window."""
    sends = itertools.count()

    def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
        now = Window(turns=[TurnRecord(ts=1.0, context={"test": test_id}, usage=on_time)])
        record = TurnRecord(ts=1.0, context={"test": test_id}, usage=late)
        return (now, Window(turns=[record])) if next(sends) == 0 else now

    return script


def _late_untagged(on_time: Usage, late: Usage) -> Callable[[str, str], Any]:
    """``_late`` for an agent that tags nothing: no record says which test it belongs to."""
    sends = itertools.count()

    def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
        now = Window(turns=[TurnRecord(ts=1.0, usage=on_time)])
        return (now, Window(turns=[TurnRecord(ts=1.0, usage=late)])) if next(sends) == 0 else now

    return script


def _untagging(*usages: Usage) -> FakeAgent:
    """``_reporting`` for an agent that tags nothing."""
    sent = itertools.count()

    def script(message: str, test_id: str) -> Window:
        usage = usages[min(next(sent), len(usages) - 1)]
        return Window(turns=[TurnRecord(ts=1.0, usage=usage)])

    return FakeAgent(script)


class _Failing(FakeAgent):
    """A ``FakeAgent`` that answers a 500 to the sends in ``failing``, counted from 0."""

    def __init__(self, script: Callable[[str, str], Any], *failing: int) -> None:
        super().__init__(script)
        self._sends = itertools.count()
        self._failing = failing
        self.failed = False

    def send(self, message: str, *, values: Values) -> str:
        if next(self._sends) in self._failing:
            self.failed = True
            raise AgentResponseError("the agent answered 500: internal error")
        return super().send(message, values=values)


class _Traced(FileTraceCollector):
    """An agent that writes real JSONL: the whole way from a line to the verdict."""

    def __init__(self, path: Path, *lines: dict[str, Any]) -> None:
        super().__init__(path)
        self._lines = lines
        self._write({"type": "hello", "sdk": "test", "pid": 1})

    def _write(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"v": 1, "ts": 1.0, **record}) + "\n")

    def send(self, message: str, *, values: Values) -> str:
        for line in self._lines:
            context = {} if "untagged" in line else {"context": {"test": values.test_id}}
            fields = {key: value for key, value in line.items() if key != "untagged"}
            self._write({"type": "turn", **context, **fields})
        return "ok"


def _tagged(usage: object = None) -> dict[str, Any]:
    return {"usage": usage}


def _untagged(usage: object) -> dict[str, Any]:
    return {"usage": usage, "untagged": True}


def _runner(
    agent: FakeAgent | _Traced | _Writing,
    *,
    reporter: MagicMock | None = None,
    managed: bool = True,
    max_cost: float | None = None,
    spent: Mapping[str, float] | None = None,
    spent_untagged: Mapping[str, float] | None = None,
    secrets: Mapping[str, str] | None = None,
    variations: Any = None,
    correlate: Mapping[str, Any] | None = None,
    session: Any = None,
) -> Runner:
    return Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=0,
        variations=variations,
        managed=managed,
        max_cost=max_cost,
        spent=spent,
        spent_untagged=spent_untagged,
        secrets=secrets,
        correlate=correlate,
        session=session,
    )


def _run(
    tests: list[Case],
    agent: FakeAgent | _Traced | _Writing,
    *,
    default: Budget | None = None,
    **options: Any,
) -> RunSummary:
    spec = Spec(
        agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
        suites=[Suite(name="S", tests=tests)],
        default_budget=default,
    )
    return _runner(agent, **options).run(spec)


def _budgeted(budget: Budget, **fields: Any) -> Case:
    return Case(trigger="t", expect=Expect(response=SAYS_OK.response, budget=budget), **fields)


class TestPerTurn:
    def test_ac6_a_turn_over_its_own_limit_fails(self) -> None:
        limit = Expect(budget=Budget(max_input_tokens=150))
        turns = [Turn(trigger="a", expect=limit), Turn(trigger="b", expect=limit)]

        (test,) = _run([Case(turns=turns)], _reporting(_tokens(100), _tokens(200))).tests

        assert test.status is Status.FAILED
        assert test.errors == (
            "turn 2/2 (b): the turn used 200 input tokens, over max_input_tokens 150",
        )

    def test_ac6_a_turn_is_held_to_its_own_usage_not_the_sum(self) -> None:
        limit = Expect(budget=Budget(max_input_tokens=150))
        turns = [Turn(trigger="a", expect=limit), Turn(trigger="b", expect=limit)]

        (test,) = _run([Case(turns=turns)], _reporting(_tokens(100), _tokens(100))).tests

        assert test.status is Status.PASSED
        assert test.totals is not None
        assert test.totals.input_tokens == 200

    def test_a_turn_over_its_limit_stops_the_conversation(self) -> None:
        turns = [
            Turn(trigger="a", expect=Expect(budget=Budget(max_input_tokens=1))),
            Turn(trigger="b", expect=SAYS_OK),
        ]
        agent = _reporting(_tokens(100))

        _run([Case(turns=turns)], agent)

        assert [message for message, _ in agent.sent] == ["a"]

    def test_ac9_a_turn_without_usage_is_unverified(self) -> None:
        turns = [Turn(trigger="a", expect=Expect(budget=Budget(max_cost=1)))]

        (test,) = _run([Case(turns=turns)], _reporting(None)).tests

        assert test.status is Status.ERROR
        assert "no usage" in test.errors[0]

    def test_a_turn_that_fails_stays_failed_without_usage(self) -> None:
        expect = Expect(response=FAILS.response, budget=Budget(max_cost=1))

        (test,) = _run([Case(turns=[Turn(trigger="a", expect=expect)])], _reporting(None)).tests

        assert test.status is Status.FAILED
        assert any("no usage" in error for error in test.errors)

    def test_the_outcome_shows_the_turns_limit(self) -> None:
        limit = Budget(max_cost=1)
        turns = [Turn(trigger="a", expect=Expect(budget=limit))]

        (test,) = _run([Case(turns=turns)], _reporting(_usd(0.5))).tests

        assert test.phrasings[0].executions[0].turns[0].budget == limit
        assert test.budget is None


class TestPerExecution:
    @pytest.mark.parametrize(
        ("used", "status"),
        [(999, Status.PASSED), (1000, Status.PASSED), (1001, Status.FAILED)],
    )
    def test_ac6_a_test_below_at_and_above_its_limit(self, used: int, status: Status) -> None:
        test = _budgeted(Budget(max_input_tokens=1000))

        (outcome,) = _run([test], _reporting(_tokens(used))).tests

        assert outcome.status is status

    def test_ac6_the_sum_of_a_conversation_is_over_while_each_turn_is_within(self) -> None:
        limit = Expect(budget=Budget(max_input_tokens=200))
        test = Case(
            expect=Expect(budget=Budget(max_input_tokens=250)),
            turns=[Turn(trigger="a", expect=limit), Turn(trigger="b", expect=limit)],
        )

        (outcome,) = _run([test], _reporting(_tokens(150), _tokens(150))).tests

        assert outcome.status is Status.FAILED
        assert outcome.errors == ("the execution used 300 input tokens, over max_input_tokens 250",)

    def test_a_conversations_budget_needs_no_expect_on_its_turns(self) -> None:
        test = Case(
            expect=Expect(budget=Budget(max_cost=0.3)),
            turns=[Turn(trigger="a", expect=Expect()), Turn(trigger="b", expect=Expect())],
        )

        (outcome,) = _run([test], _reporting(_usd(0.1), _usd(0.2))).tests

        assert outcome.status is Status.PASSED  # equal to the limit, in exact arithmetic

    def test_ac7_cached_input_is_counted_in_the_limit(self) -> None:
        test = _budgeted(Budget(max_input_tokens=900))

        (outcome,) = _run([test], _reporting(_tokens(1000, cached=800))).tests

        assert outcome.status is Status.FAILED
        assert "1000 input tokens (800 of them cached)" in outcome.errors[0]

    def test_ac8_one_run_over_budget_stays_within_min_pass(self) -> None:
        test = _budgeted(Budget(max_input_tokens=500), runs=3, min_pass=2)

        (outcome,) = _run([test], _reporting(_tokens(1000), _tokens(1), _tokens(1))).tests

        assert outcome.status is Status.PASSED
        assert outcome.counts == Counts(passed=2, failed=1, error=0)

    def test_ac8_two_runs_over_budget_fail_it(self) -> None:
        test = _budgeted(Budget(max_input_tokens=500), runs=3, min_pass=2)

        (outcome,) = _run([test], _reporting(_tokens(1000), _tokens(1000), _tokens(1))).tests

        assert outcome.status is Status.FAILED
        assert outcome.counts == Counts(passed=1, failed=2, error=0)

    def test_each_phrasing_is_held_to_it_on_its_own(self) -> None:
        generator = MagicMock()
        generator.generate.return_value = ["v1"]
        test = _budgeted(Budget(max_input_tokens=500), variations=1)
        agent = _reporting(_tokens(1), _tokens(1000))

        (outcome,) = _run([test], agent, variations=generator).tests

        assert outcome.status is Status.FAILED
        assert [p.status for p in outcome.phrasings] == [Status.PASSED, Status.FAILED]

    def test_ac9_without_usage_it_is_unverified_never_failed_nor_passed(self) -> None:
        (outcome,) = _run([_budgeted(Budget(max_cost=1))], _reporting(None)).tests

        assert outcome.status is Status.ERROR
        assert outcome.counts == Counts(passed=0, failed=0, error=1)
        assert "no usage" in outcome.errors[0]
        assert "trace contract" in outcome.errors[0]

    def test_ac9_a_budget_does_not_turn_a_failure_into_an_error(self) -> None:
        test = Case(trigger="t", expect=Expect(response=FAILS.response, budget=Budget(max_cost=1)))

        (outcome,) = _run([test], _reporting(None)).tests

        assert outcome.status is Status.FAILED

    def test_known_turns_over_the_limit_fail_it_though_a_turn_has_no_usage(self) -> None:
        """A failure is a failure (combine()): what is missing can only add to the sum."""
        test = Case(
            expect=Expect(budget=Budget(max_input_tokens=50)),
            turns=[Turn(trigger="a", expect=Expect()), Turn(trigger="b", expect=Expect())],
        )

        (outcome,) = _run([test], _reporting(_tokens(100), None)).tests

        assert outcome.status is Status.FAILED

    def test_known_turns_within_the_limit_prove_nothing(self) -> None:
        test = Case(
            expect=Expect(budget=Budget(max_input_tokens=500)),
            turns=[Turn(trigger="a", expect=Expect()), Turn(trigger="b", expect=Expect())],
        )

        (outcome,) = _run([test], _reporting(_tokens(100), None)).tests

        assert outcome.status is Status.ERROR

    def test_a_cost_the_table_cannot_give_leaves_the_budget_unverified(self) -> None:
        agent = _reporting(Usage(model="my-model", input_tokens=5, output_tokens=5))
        test = _budgeted(Budget(max_cost=1))

        summary = _run([test], agent)

        assert summary.tests[0].status is Status.ERROR
        assert "cannot check max_cost" in summary.tests[0].errors[0]

    def test_a_budget_holds_against_the_cost_from_the_table(self) -> None:
        usage = Usage(model="gpt-4o-mini", provider="openai", input_tokens=1000, output_tokens=200)

        (outcome,) = _run([_budgeted(Budget(max_cost=0.0002))], _reporting(usage)).tests

        assert outcome.status is Status.FAILED
        assert outcome.errors == ("the execution cost 0.00027 USD, over max_cost 0.0002",)

    def test_a_late_call_still_fails_a_run_within_its_budget(self) -> None:
        """The budget is checked once, and a late call judges the run again like any other."""

        def script(message: str, test_id: str) -> tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_tokens(1))
            call = _call("refund", test_id)
            return Window(turns=[turn]), Window(tools=[call])

        expect = Expect(forbid=("refund",), budget=Budget(max_input_tokens=100))

        (outcome,) = _run([Case(trigger="t", expect=expect)], FakeAgent(script)).tests

        assert outcome.status is Status.FAILED
        assert "refund" in outcome.errors[0]

    def test_a_budget_within_its_limit_does_not_hide_a_failed_check(self) -> None:
        expect = Expect(function_calls=[FunctionCallExpect(name="x")], budget=Budget(max_cost=1))

        (outcome,) = _run([Case(trigger="t", expect=expect)], _reporting(_usd(0.1))).tests

        assert outcome.status is Status.FAILED


class TestDefaults:
    def test_ac10_the_default_applies_to_a_test_without_its_own(self) -> None:
        default = Budget(max_input_tokens=500)

        (outcome,) = _run([PLAIN], _reporting(_tokens(1000)), default=default).tests

        assert outcome.status is Status.FAILED
        assert outcome.budget == default

    def test_ac10_the_tests_own_budget_prevails(self) -> None:
        default, own = Budget(max_input_tokens=500), Budget(max_input_tokens=2000)
        tests = [Case(trigger="a", expect=SAYS_OK), _budgeted(own)]

        first, second = _run(tests, _reporting(_tokens(1000)), default=default).tests

        assert (first.status, first.budget) == (Status.FAILED, default)
        assert (second.status, second.budget) == (Status.PASSED, own)

    def test_ac10_a_tests_budget_replaces_the_default_whole(self) -> None:
        """A limit the test left out is not taken from the default."""
        default = Budget(max_input_tokens=1, max_cost=100)
        test = _budgeted(Budget(max_cost=1))

        (outcome,) = _run([test], _reporting(_usd(0.1)), default=default).tests

        assert outcome.status is Status.PASSED

    def test_ac10_it_covers_a_whole_conversation(self) -> None:
        turns = [Turn(trigger="a", expect=SAYS_OK), Turn(trigger="b", expect=SAYS_OK)]
        default = Budget(max_input_tokens=500)

        (outcome,) = _run([Case(turns=turns)], _reporting(_tokens(300)), default=default).tests

        assert outcome.status is Status.FAILED
        assert "600 input tokens" in outcome.errors[0]

    def test_a_limit_on_a_turn_does_not_replace_the_default(self) -> None:
        limit = Expect(budget=Budget(max_input_tokens=400))
        turns = [Turn(trigger="a", expect=limit), Turn(trigger="b", expect=limit)]
        default = Budget(max_input_tokens=500)

        (outcome,) = _run([Case(turns=turns)], _reporting(_tokens(300)), default=default).tests

        assert outcome.status is Status.FAILED

    def test_ac9_the_default_without_usage_is_unverified(self) -> None:
        (outcome,) = _run([PLAIN], _reporting(None), default=Budget(max_cost=1)).tests

        assert outcome.status is Status.ERROR

    def test_the_default_counts_in_the_definition_like_one_written_in_the_test(self) -> None:
        budget = Budget(max_cost=1)

        (inherited,) = _run([PLAIN], _reporting(_usd(0.1)), default=budget).tests
        (written,) = _run([_budgeted(budget)], _reporting(_usd(0.1))).tests
        (without,) = _run([PLAIN], _reporting(_usd(0.1))).tests

        assert inherited.definition_sha256 == written.definition_sha256
        assert inherited.definition_sha256 != without.definition_sha256

    def test_a_default_budget_needs_an_agent_that_writes_traces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing else in the spec asks for a `hello`: the default has to."""
        monkeypatch.setattr("highbar.engine.observation.HELLO_GRACE_S", 0.0)

        with pytest.raises(AgentNotObservedError, match="nothing was written"):
            _run([PLAIN], FakeAgent(hello=False), default=Budget(max_cost=1))


class TestTotals:
    def test_ac4_every_level_adds_up(self) -> None:
        spec = Spec(
            agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
            suites=[
                Suite(
                    name="A",
                    tests=[
                        Case(trigger="a1", expect=SAYS_OK, runs=2),
                        Case(trigger="a2", expect=SAYS_OK),
                    ],
                ),
                Suite(name="B", tests=[Case(trigger="b1", expect=SAYS_OK)]),
            ],
        )

        summary = _runner(_reporting(_usd(0.01))).run(spec)

        first, second, third = summary.tests
        one = first.phrasings[0].executions[0]
        assert one.turns[0].usage == _usd(0.01)
        assert one.totals is not None
        assert (one.totals.turns, one.totals.input_tokens) == (1, 100)
        assert one.totals.cost == {"USD": 0.01}
        assert first.totals is not None
        assert (first.totals.input_tokens, first.totals.cost) == (200, {"USD": 0.02})
        assert second.totals is not None
        assert third.totals is not None
        suite_a, suite_b = summary.suites
        assert (suite_a.name, suite_b.name) == ("A", "B")
        assert suite_a.totals is not None
        assert suite_b.totals is not None
        assert (suite_a.totals.turns, suite_a.totals.cost) == (3, {"USD": 0.03})
        assert (suite_b.totals.turns, suite_b.totals.cost) == (1, {"USD": 0.01})
        assert summary.totals is not None
        assert (summary.totals.turns, summary.totals.input_tokens) == (4, 400)
        assert summary.totals.output_tokens == 40
        assert summary.totals.cost == {"USD": 0.04}
        assert summary.totals.complete is True

    def test_ac1_an_agent_without_usage_owes_nothing(self) -> None:
        summary = _run([Case(trigger="t", expect=SAYS_OK)], _reporting(None))

        (turn,) = summary.tests[0].phrasings[0].executions[0].turns
        assert turn.usage is None
        assert summary.tests[0].totals is None
        assert summary.suites[0].totals is None
        assert summary.totals is None
        assert summary.warnings == ()

    def test_ac5_the_sum_says_it_is_incomplete_at_each_level(self) -> None:
        tests = [Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, _reporting(_usd(0.5), None))

        first, second = summary.tests
        assert first.totals is not None
        assert first.totals.complete is True
        assert second.totals is None
        assert summary.totals is not None
        assert summary.totals.cost == {"USD": 0.5}
        assert summary.totals.warnings == ("1 of 2 turns with no usage",)
        assert summary.suites[0].totals == summary.totals

    def test_ac3_an_unknown_model_warns_once(self) -> None:
        reporter = MagicMock()
        usage = Usage(model="my-model", input_tokens=5, output_tokens=5)
        tests = [Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, _reporting(usage), reporter=reporter)

        (warning,) = summary.warnings
        assert "my-model" in warning
        assert reporter.on_warning.call_count == 1

    def test_ac3_a_cost_from_the_table_reaches_the_turn(self) -> None:
        usage = Usage(
            model="gpt-4o-mini",
            provider="openai",
            input_tokens=1000,
            output_tokens=200,
            cached_input_tokens=800,
        )

        (test,) = _run([Case(trigger="t", expect=SAYS_OK)], _reporting(usage)).tests

        (turn,) = test.phrasings[0].executions[0].turns
        assert turn.usage is not None
        assert (turn.usage.cost, turn.usage.currency) == (0.00021, "USD")

    def test_a_failed_test_still_spent_it(self) -> None:
        summary = _run([Case(trigger="t", expect=FAILS)], _reporting(_usd(0.5)))

        assert summary.totals is not None
        assert summary.totals.cost == {"USD": 0.5}

    def test_the_records_of_one_message_add_up(self) -> None:
        """The middleware's turn has no usage, and two `hb.turn()` each have some."""

        def script(message: str, test_id: str) -> Window:
            context = {"test": test_id}
            return Window(
                turns=[
                    TurnRecord(ts=1.0, context=context),
                    TurnRecord(ts=1.0, context=context, usage=_usd(0.1)),
                    TurnRecord(ts=1.0, context=context, usage=_usd(0.2)),
                ]
            )

        (test,) = _run([_budgeted(Budget(max_cost=0.2))], FakeAgent(script)).tests

        (turn,) = test.phrasings[0].executions[0].turns
        assert turn.usage == Usage(input_tokens=200, output_tokens=20, cost=0.3, currency="USD")
        assert test.status is Status.FAILED

    def test_another_tests_record_is_not_counted(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(
                turns=[
                    TurnRecord(ts=1.0, context={"test": test_id}, usage=_usd(0.1)),
                    TurnRecord(ts=1.0, context={"test": "someone-else"}, usage=_usd(9.0)),
                ]
            )

        (test,) = _run([Case(trigger="t", expect=SAYS_OK)], FakeAgent(script)).tests

        assert test.totals is not None
        assert test.totals.cost == {"USD": 0.1}

    def test_q4_a_late_turn_counts_for_the_test_that_made_it(self) -> None:
        tests = [Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, FakeAgent(_late(None, _usd(5.0))))

        first, second = summary.tests
        assert first.totals is not None
        assert first.totals.cost == {"USD": 5.0}
        assert second.totals is None
        assert summary.totals is not None
        assert summary.totals.warnings == ("1 of 2 turns with no usage",)

    def test_a_managed_agent_without_context_counts_what_is_there(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(turns=[TurnRecord(ts=1.0, usage=_usd(0.1))])

        (test,) = _run([Case(trigger="t", expect=SAYS_OK)], FakeAgent(script)).tests

        assert test.totals is not None
        assert test.totals.cost == {"USD": 0.1}

    def test_an_attached_agent_without_context_may_be_serving_others(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(turns=[TurnRecord(ts=1.0, usage=_usd(0.1))])

        summary = _run([Case(trigger="t", expect=SAYS_OK)], FakeAgent(script), managed=False)

        assert summary.tests[0].status is Status.PASSED
        assert summary.totals is None

    def test_an_attached_budget_without_context_is_not_observed(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(turns=[TurnRecord(ts=1.0, usage=_usd(0.1))])

        (test,) = _run([_budgeted(Budget(max_cost=1))], FakeAgent(script), managed=False).tests

        assert test.status is Status.ERROR
        assert "not observed" in test.errors[0]

    def test_nothing_from_a_var_reaches_the_usage(self) -> None:
        usage = Usage(model=TOKEN, provider=TOKEN, cost=1.0, currency=TOKEN)

        summary = _run(
            [Case(trigger="t", expect=SAYS_OK)],
            _reporting(usage),
            secrets={TOKEN: "AGENT_TOKEN"},
        )

        assert TOKEN not in repr(summary)
        (turn,) = summary.tests[0].phrasings[0].executions[0].turns
        assert turn.usage is not None
        assert (turn.usage.model, turn.usage.currency) == ("${AGENT_TOKEN}", "${AGENT_TOKEN}")
        assert summary.totals is not None
        assert summary.totals.cost == {"${AGENT_TOKEN}": 1.0}

    def test_nothing_from_a_var_reaches_a_sum_through_a_record(self) -> None:
        records = (Usage(cost=1.0, currency=TOKEN), Usage(cost=2.0, currency="USD"))
        agent = _spending(records, stray=Usage(model=TOKEN, cost=1.0, currency=TOKEN))

        summary = _run([Case(trigger="t", expect=SAYS_OK)], agent, secrets={TOKEN: "AGENT_TOKEN"})

        assert TOKEN not in repr(summary)
        assert summary.totals is not None
        assert summary.totals.cost == {"${AGENT_TOKEN}": 1.0, "USD": 2.0}


class TestContradictoryUsage:
    """Q5: provider counters copied as they come, with the cached tokens left out of the input."""

    COPIED = {
        "model": "gpt-4o-mini",
        "provider": "openai",
        "input_tokens": 12,
        "output_tokens": 1,
        "cached_input_tokens": 50_000,
    }

    def test_q5_it_cannot_pass_a_limit_on_tokens(self, tmp_path: Path) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _tagged(self.COPIED))

        summary = _run([_budgeted(Budget(max_input_tokens=1000))], agent)

        (test,) = summary.tests
        assert test.status is Status.ERROR
        assert any("cached_input_tokens" in warning for warning in summary.warnings)

    def test_q5_it_cannot_pass_a_limit_on_cost(self, tmp_path: Path) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _tagged(self.COPIED))

        (test,) = _run([_budgeted(Budget(max_cost=0.01))], agent).tests

        assert test.status is Status.ERROR

    def test_q5_its_sums_say_they_are_incomplete(self, tmp_path: Path) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _tagged(self.COPIED))

        summary = _run([Case(trigger="t", expect=SAYS_OK)], agent)

        assert summary.totals is not None
        assert summary.totals.complete is False


class TestUntaggedUsage:
    """Q1: `turn` records that lost their context, next to one that kept it."""

    def test_q1_it_counts_for_the_test_when_nothing_is_tagged(self, tmp_path: Path) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _untagged({"input_tokens": 999_999}))

        (test,) = _run([_budgeted(Budget(max_input_tokens=150))], agent).tests

        assert test.status is Status.FAILED

    def test_q1_tokens_that_would_break_a_budget_leave_it_unverified(self, tmp_path: Path) -> None:
        agent = _Traced(
            tmp_path / "t.jsonl",
            _tagged({"input_tokens": 100}),
            _untagged({"input_tokens": 999_999}),
        )

        summary = _run([_budgeted(Budget(max_input_tokens=150))], agent)

        (test,) = summary.tests
        assert test.status is Status.ERROR
        assert "cannot tell which test" in test.errors[0]
        assert any("no test context" in warning for warning in summary.warnings)

    def test_q1_a_cost_that_would_break_a_budget_leaves_it_unverified(self, tmp_path: Path) -> None:
        agent = _Traced(
            tmp_path / "t.jsonl",
            _tagged({"input_tokens": 1, "output_tokens": 1, "cost": 0.1, "currency": "USD"}),
            _untagged({"input_tokens": 1, "output_tokens": 1, "cost": 9, "currency": "USD"}),
        )

        (test,) = _run([_budgeted(Budget(max_cost=1))], agent).tests

        assert test.status is Status.ERROR
        assert test.totals is not None
        assert test.totals.cost == {"USD": 0.1}
        assert test.totals.complete is False

    def test_q1_it_holds_for_a_limit_on_a_turn(self) -> None:
        limit = Expect(budget=Budget(max_input_tokens=150))
        agent = _spending((_tokens(100),), stray=_tokens(999_999))

        (test,) = _run([Case(turns=[Turn(trigger="a", expect=limit)])], agent).tests

        assert test.status is Status.ERROR
        assert "the turn keeps to its budget only without it" in test.errors[0]

    def test_q1_it_holds_for_the_sum_of_a_conversation(self) -> None:
        test = Case(
            expect=Expect(budget=Budget(max_input_tokens=300)),
            turns=[Turn(trigger="a", expect=Expect()), Turn(trigger="b", expect=Expect())],
        )
        agent = _spending((_tokens(100),), stray=_tokens(150))

        (outcome,) = _run([test], agent).tests

        assert outcome.status is Status.ERROR
        assert "the execution keeps to its budget only without it" in outcome.errors[0]

    def test_q1_usage_that_changes_nothing_warns_and_leaves_the_sum_partial(self) -> None:
        agent = _spending((_tokens(100),), stray=_tokens(10))
        reporter = MagicMock()

        summary = _run([_budgeted(Budget(max_input_tokens=150))], agent, reporter=reporter)

        (test,) = summary.tests
        assert test.status is Status.PASSED
        assert test.totals is not None
        assert test.totals.input_tokens == 100
        assert test.totals.complete is False
        assert (
            "1 of 1 turn next to usage with no test context, left out of the sum"
            in test.totals.warnings
        )
        (warning,) = summary.warnings
        assert "count for no test" in warning
        assert reporter.on_warning.call_count == 1

    def test_q1_a_tagged_turn_with_no_usage_of_its_own(self, tmp_path: Path) -> None:
        """The middleware tags its `turn`, and a worker's `hb.turn()` has the usage."""
        agent = _Traced(
            tmp_path / "t.jsonl",
            _tagged(),
            _untagged({"input_tokens": 1, "output_tokens": 1, "cost": 9, "currency": "USD"}),
        )

        (test,) = _run([Case(trigger="t", expect=SAYS_OK)], agent).tests

        assert test.status is Status.PASSED
        assert test.totals is not None
        assert test.totals.cost == {}
        assert test.totals.complete is False

    def test_q1_that_turn_cannot_pass_a_budget(self, tmp_path: Path) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _tagged(), _untagged({"input_tokens": 1}))

        (test,) = _run([_budgeted(Budget(max_input_tokens=150))], agent).tests

        assert test.status is Status.ERROR

    def test_q1_an_unreadable_one_is_no_more_a_pass(self, tmp_path: Path) -> None:
        agent = _Traced(
            tmp_path / "t.jsonl",
            _tagged({"input_tokens": 100}),
            _untagged({"input_tokens": "999999"}),
        )

        (test,) = _run([_budgeted(Budget(max_input_tokens=150))], agent).tests

        assert test.status is Status.ERROR

    def test_q1_a_failure_stays_a_failure(self) -> None:
        agent = _spending((_tokens(200),), stray=_tokens(999_999))

        (test,) = _run([_budgeted(Budget(max_input_tokens=150))], agent).tests

        assert test.status is Status.FAILED

    def test_q1_a_ceiling_counts_it_when_highbar_started_the_agent(self) -> None:
        """It can only be this run's, so the run stops, and the totals stay as they were."""
        agent = _spending((_usd(0.1),), stray=_usd(9.0))
        tests = [Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, agent, max_cost=1)

        assert [message for message, _ in agent.sent] == ["a"]
        assert summary.stopped is not None
        assert "9.1 USD (9.0 USD of it with no test context, in no total)" in summary.stopped
        assert summary.totals is not None
        assert summary.totals.cost == {"USD": 0.1}
        assert summary.totals.complete is False
        assert summary.untagged_cost == {"USD": 9.0}
        assert not any("counted only" in warning for warning in summary.warnings)

    def test_q6_a_ceiling_counts_usage_tagged_for_no_test_of_the_run(self) -> None:
        """A managed agent's spend is this run's, whatever context the record carries."""

        def script(message: str, test_id: str) -> Window:
            return Window(
                turns=[
                    TurnRecord(ts=1.0, context={"test": test_id}, usage=_usd(0.1)),
                    TurnRecord(ts=1.0, context={"test": "someone-else"}, usage=_usd(9.0)),
                ]
            )

        agent = FakeAgent(script)
        tests = [Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, agent, max_cost=1)

        assert [message for message, _ in agent.sent] == ["a"]
        assert summary.stopped is not None
        assert "9.1 USD (9.0 USD of it with no test context, in no total)" in summary.stopped
        assert summary.totals is not None
        assert summary.totals.cost == {"USD": 0.1}
        assert summary.untagged_cost == {"USD": 9.0}

    def test_q6_that_usage_stays_out_for_an_attached_agent(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(
                turns=[
                    TurnRecord(ts=1.0, context={"test": test_id}, usage=_usd(0.1)),
                    TurnRecord(ts=1.0, context={"test": "someone-else"}, usage=_usd(9.0)),
                ]
            )

        summary = _run([PLAIN], FakeAgent(script), managed=False, max_cost=1)

        assert summary.stopped is None
        assert summary.untagged_cost == {}

    def test_q6_a_ceiling_warns_when_that_usage_has_no_cost(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(
                turns=[
                    TurnRecord(ts=1.0, context={"test": test_id}, usage=_usd(0.1)),
                    TurnRecord(ts=1.0, context={"test": "someone-else"}, usage=_tokens(5)),
                ]
            )

        summary = _run([PLAIN], FakeAgent(script), max_cost=5)

        assert any("--max-cost 5 counted only" in warning for warning in summary.warnings)

    def test_q1_a_ceiling_leaves_it_out_for_an_attached_agent(self) -> None:
        """It may be someone else's: the run goes on, and says what it could not count."""
        agent = _spending((_usd(0.1),), stray=_usd(9.0))
        tests = [Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, agent, managed=False, max_cost=1)

        assert summary.stopped is None
        assert any("--max-cost 1 counted only" in warning for warning in summary.warnings)
        assert any("2 of 2 turns" in warning for warning in summary.warnings)
        assert summary.totals is not None
        assert summary.totals.complete is False
        assert summary.untagged_cost == {}

    def test_q1_a_ceiling_counts_it_once(self) -> None:
        """It sits on the turn it was read in and on the last turn of the test before."""
        agent = _spending((_usd(0.1),), stray=_usd(0.4))
        tests = [Case(trigger=name, expect=SAYS_OK) for name in "abc"]

        summary = _run(tests, agent, max_cost=1.3)

        assert [message for message, _ in agent.sent] == ["a", "b", "c"]
        assert summary.stopped is not None
        assert "1.5 USD (1.2 USD of it" in summary.stopped

    def test_q1_a_ceiling_counts_it_after_the_last_test(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_usd(0.1))
            return Window(turns=[turn]), Window(turns=[TurnRecord(ts=1.0, usage=_usd(5.0))])

        summary = _run([_budgeted(Budget(max_cost=100))], FakeAgent(script), max_cost=1)

        assert summary.stopped is not None
        assert "5.1 USD (5.0 USD of it" in summary.stopped

    def test_q1_a_ceiling_still_warns_of_one_with_no_cost(self) -> None:
        agent = _spending((_usd(0.1),), stray=_tokens(10))

        summary = _run([Case(trigger="t", expect=SAYS_OK)], agent, max_cost=5)

        assert summary.stopped is None
        assert any(
            "--max-cost 5 counted only" in warning and "no test context and no cost" in warning
            for warning in summary.warnings
        )

    def test_q1_the_stop_message_says_it_of_the_currency_that_stopped_the_run(self) -> None:
        agent = _spending((_usd(2.0),), stray=Usage(cost=0.5, currency="EUR"))

        summary = _run([Case(trigger="t", expect=SAYS_OK)], agent, max_cost=1)

        assert summary.stopped is not None
        assert "2.0 USD by the end of" in summary.stopped
        assert "no test context" not in summary.stopped

    def test_q1_a_stray_of_an_earlier_spec_stays_in_the_stop_message(self) -> None:
        for managed in (True, False):
            summary = _run(
                [Case(trigger="t", expect=SAYS_OK)],
                _reporting(_usd(0.01)),
                managed=managed,
                max_cost=0.025,
                spent={"USD": 0.02},
                spent_untagged={"USD": 0.02},
            )

            assert summary.stopped is not None
            assert "0.03 USD (0.02 USD of it" in summary.stopped

    def test_q1_it_may_be_the_late_usage_of_the_test_before(self) -> None:
        def script(message: str, test_id: str) -> Window:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_tokens(100))
            stray = TurnRecord(ts=1.0, usage=_tokens(999_999))
            return Window(turns=[turn, *([stray] if message == "b" else [])])

        tests = [_budgeted(Budget(max_input_tokens=150)), Case(trigger="b", expect=SAYS_OK)]

        first, second = _run(tests, FakeAgent(script)).tests

        assert first.status is Status.ERROR
        assert "cannot tell which test" in first.errors[0]
        assert second.status is Status.PASSED

    def test_q1_it_may_arrive_after_the_last_test(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_tokens(100))
            return Window(turns=[turn]), Window(turns=[TurnRecord(ts=1.0, usage=_tokens(999_999))])

        summary = _run([_budgeted(Budget(max_input_tokens=150))], FakeAgent(script))

        (test,) = summary.tests
        assert test.status is Status.ERROR
        assert any("no test context" in warning for warning in summary.warnings)

    def test_a_turn_with_no_usage_says_nothing(self, tmp_path: Path) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _tagged({"input_tokens": 100}), _untagged(None))

        summary = _run([_budgeted(Budget(max_input_tokens=150))], agent)

        assert summary.tests[0].status is Status.PASSED
        assert summary.warnings == ()

    def test_q8_an_agent_that_tags_nothing_is_told_the_test_before_answers_for_it(self) -> None:
        """Its usage goes to the test whose window it falls in, which may not be its own."""
        tests = [_budgeted(Budget(max_input_tokens=100)), Case(trigger="b", expect=SAYS_OK)]
        agent = FakeAgent(_late_untagged(_tokens(90), _tokens(90)))

        summary = _run(tests, agent)

        (warning,) = [w for w in summary.warnings if "the test whose window it falls in" in w]
        assert "is an error if its budget holds only without it" in warning
        assert "hb.turn(test=" in warning

    @pytest.mark.parametrize("followed", [False, True])
    def test_q8_a_late_turn_gives_one_result_wherever_the_test_runs(self, followed: bool) -> None:
        """Last or followed by another, a green on one side would depend on the order."""
        tests = [_budgeted(Budget(max_input_tokens=100))]
        tests += [Case(trigger="b", expect=SAYS_OK)] if followed else []

        summary = _run(tests, FakeAgent(_late_untagged(_tokens(90), _tokens(90))))

        assert summary.tests[0].status is Status.ERROR

    def test_q8_neighbours_over_a_limit_together_need_nothing_late(self, tmp_path: Path) -> None:
        """The accepted cost of tagging nothing, so the message says how to get out of it."""
        agent = _Traced(tmp_path / "t.jsonl", _untagged({"input_tokens": 60}))
        tests = [_budgeted(Budget(max_input_tokens=100)), Case(trigger="b", expect=SAYS_OK)]

        first, second = _run(tests, agent).tests

        assert first.status is Status.ERROR
        (error,) = first.errors
        assert "cannot tell which test spent the usage read in the next test's window" in error
        assert "carry no test context" in error
        assert "hb.turn(test=<the X-Highbar-Test header>), and each counts" in error
        assert second.status is Status.PASSED

    def test_q8_neighbours_within_the_limit_pass(self) -> None:
        tests = [_budgeted(Budget(max_input_tokens=100)), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, _untagging(_tokens(50)))

        assert [test.status for test in summary.tests] == [Status.PASSED, Status.PASSED]

    def test_q8_the_message_names_the_keys_of_agent_correlate(self) -> None:
        tests = [_budgeted(Budget(max_input_tokens=100)), Case(trigger="b", expect=SAYS_OK)]
        correlate = {"conversation_id": "{{test.id}}"}

        first, _ = _run(tests, _untagging(_tokens(60)), correlate=correlate).tests

        assert first.status is Status.ERROR
        assert "none of agent.correlate's keys (conversation_id)" in first.errors[0]
        assert "hb.turn(conversation_id=...)" in first.errors[0]

    def test_q8_the_sums_stay_where_the_usage_was_read(self) -> None:
        """It counts for the test that read it, and the suspicion adds nothing to the other."""
        tests = [_budgeted(Budget(max_cost=1)), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, _untagging(_usd(0.6)))

        first, second = summary.tests
        assert first.status is Status.ERROR
        assert first.totals is not None
        assert first.totals.cost == {"USD": 0.6}
        assert first.totals.complete is True
        assert second.totals is not None
        assert second.totals.cost == {"USD": 0.6}
        assert summary.totals is not None
        assert summary.totals.cost == {"USD": 1.2}
        assert summary.totals.complete is True
        assert summary.untagged_cost == {}

    def test_q8_a_ceiling_counts_that_usage_once(self) -> None:
        tests = [_budgeted(Budget(max_cost=1)), Case(trigger="b", expect=SAYS_OK)]

        within = _run(tests, _untagging(_usd(0.6)), max_cost=1.3)
        over = _run(tests, _untagging(_usd(0.6)), max_cost=1.1)

        assert within.stopped is None
        assert over.stopped is not None
        assert "the run spent 1.2 USD by the end of" in over.stopped

    def test_q8_a_failure_stays_a_failure(self) -> None:
        tests = [_budgeted(Budget(max_input_tokens=100)), Case(trigger="b", expect=SAYS_OK)]

        first, _ = _run(tests, _untagging(_tokens(200))).tests

        assert first.status is Status.FAILED

    def test_q8_a_test_with_no_budget_answers_for_nothing(self) -> None:
        tests = [Case(trigger=name, expect=SAYS_OK) for name in "ab"]

        summary = _run(tests, _untagging(_tokens(999_999)))

        assert [test.status for test in summary.tests] == [Status.PASSED, Status.PASSED]

    def test_q8_it_holds_for_a_limit_on_a_turn(self) -> None:
        limit = Expect(budget=Budget(max_input_tokens=100))
        tests = [Case(turns=[Turn(trigger="a", expect=limit)]), Case(trigger="b", expect=SAYS_OK)]

        first, _ = _run(tests, _untagging(_tokens(60))).tests

        assert first.status is Status.ERROR

    def test_q8_a_conversation_after_it_counts_whole(self) -> None:
        """Neither of its turns is enough alone: together, the two tests are over the limit."""
        turns = [Turn(trigger=name, expect=SAYS_OK) for name in "bc"]
        tests = [_budgeted(Budget(max_input_tokens=100)), Case(turns=turns)]

        first, _ = _run(tests, _untagging(_tokens(60), _tokens(30))).tests

        assert first.status is Status.ERROR

    def test_q8_it_adds_to_what_was_read_next_to_tagged_records(self) -> None:
        """The next test's first window has no tag, and its second has one, next to a stray."""
        sends = itertools.count()

        def script(message: str, test_id: str) -> Window:
            sent = next(sends)
            tagged = TurnRecord(ts=1.0, context={"test": test_id})
            stray = TurnRecord(ts=1.0, usage=_tokens(30))
            if sent == 0:
                return Window(turns=[replace(tagged, usage=_tokens(60))])
            return Window(turns=[stray] if sent == 1 else [tagged, stray])

        turns = [Turn(trigger=name, expect=SAYS_OK) for name in "bc"]
        tests = [_budgeted(Budget(max_input_tokens=100)), Case(turns=turns)]

        first, _ = _run(tests, FakeAgent(script)).tests

        assert first.status is Status.ERROR

    def test_q8_the_runs_of_a_test_are_neighbours(self) -> None:
        tests = [_budgeted(Budget(max_input_tokens=100), runs=2)]

        (test,) = _run(tests, _untagging(_tokens(60))).tests

        assert test.status is Status.ERROR
        assert "(run 1 of 2)" in test.errors[0]

    def test_q8_min_pass_absorbs_a_run_left_unverified(self) -> None:
        tests = [_budgeted(Budget(max_input_tokens=100), runs=2, min_pass=1)]

        (test,) = _run(tests, _untagging(_tokens(60))).tests

        assert test.status is Status.PASSED

    @pytest.mark.parametrize(
        ("second", "status"),
        [
            (Case(trigger="b", expect=SAYS_OK), Status.PASSED),
            (_budgeted(Budget(max_input_tokens=100)), Status.ERROR),
        ],
    )
    def test_q8_an_attached_agent_answers_for_it_too(self, second: Case, status: Status) -> None:
        """Its window counts for no test, and a late `turn` that lost its tag can sit there."""
        sends = itertools.count()

        def script(message: str, test_id: str) -> Window:
            context = {} if next(sends) else {"test": test_id}
            return Window(turns=[TurnRecord(ts=1.0, context=context, usage=_tokens(60))])

        tests = [_budgeted(Budget(max_input_tokens=100)), second]

        first, other = _run(tests, FakeAgent(script), managed=False).tests

        assert first.status is Status.ERROR
        assert "hb.turn(test=" in first.errors[0]
        assert other.status is status

    def test_q8_no_late_turn_is_needed_for_the_warning(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(turns=[TurnRecord(ts=1.0, usage=_usd(0.1))])

        (warning,) = _run([PLAIN], FakeAgent(script)).warnings

        assert "each counts for the test whose window it falls in" in warning

    def test_q8_an_unreadable_usage_is_warned_of_too(self, tmp_path: Path) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _untagged({"input_tokens": "999999"}))

        (warning,) = _run([PLAIN], agent).warnings

        assert "each counts for the test whose window it falls in" in warning

    def test_q8_an_attached_agent_is_not_told_its_usage_counted(self) -> None:
        """Its usage counts for no test, so that warning would say what did not happen."""

        def script(message: str, test_id: str) -> Window:
            return Window(turns=[TurnRecord(ts=1.0, usage=_usd(0.1))])

        assert _run([PLAIN], FakeAgent(script), managed=False).warnings == ()

    def test_q8_a_turn_with_no_usage_says_nothing(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(turns=[TurnRecord(ts=1.0)])

        assert _run([PLAIN], FakeAgent(script)).warnings == ()


class TestNoWindowRead:
    """Q9: an execution that read no window did not see what the one before left late, so
    what comes next is still that one's to answer for."""

    LIMIT = Budget(max_input_tokens=100)

    def _neighbours(self) -> list[Case]:
        return [_budgeted(self.LIMIT), Case(trigger="b", expect=SAYS_OK)]

    def test_q9_a_failed_send_does_not_hide_late_usage_from_its_budget(self) -> None:
        agent = _Failing(_late_untagged(_tokens(90), _tokens(90)), 1)

        first, second = _run(self._neighbours(), agent).tests

        assert first.status is Status.ERROR
        assert "cannot tell which test" in first.errors[0]
        assert second.status is Status.ERROR
        assert second.errors == ("the agent answered 500: internal error",)

    def test_q9_min_pass_does_not_turn_it_green(self) -> None:
        """The run that failed to send is not the run that may be over its budget."""
        tests = [_budgeted(self.LIMIT, runs=2, min_pass=1)]
        agent = _Failing(_late_untagged(_tokens(90), _tokens(90)), 1)

        summary = _run(tests, agent)

        (test,) = summary.tests
        assert test.status is Status.ERROR
        assert test.counts == Counts(passed=0, failed=0, error=2)
        assert summary.passed == 0

    def test_q9_late_usage_within_the_limit_changes_nothing(self) -> None:
        tests = [_budgeted(self.LIMIT, runs=2, min_pass=1)]

        (test,) = _run(tests, _Failing(_late_untagged(_tokens(90), _tokens(5)), 1)).tests

        assert test.status is Status.PASSED

    def test_q9_the_test_after_it_reads_the_late_usage(self) -> None:
        tests = [*self._neighbours(), Case(trigger="c", expect=SAYS_OK)]
        agent = _Failing(_late_untagged(_tokens(90), _tokens(90)), 1)

        first, second, third = _run(tests, agent).tests

        assert first.status is Status.ERROR
        assert "in the next test's window" in first.errors[0]
        assert second.status is Status.ERROR
        assert third.status is Status.PASSED

    @pytest.mark.parametrize("readers", [0, 1])
    def test_q9_more_failed_sends_change_nothing(self, readers: int) -> None:
        tests = [*self._neighbours(), Case(trigger="c", expect=SAYS_OK)]
        tests += [Case(trigger="d", expect=SAYS_OK)] * readers
        agent = _Failing(_late_untagged(_tokens(90), _tokens(90)), 1, 2)

        summary = _run(tests, agent)

        assert summary.tests[0].status is Status.ERROR

    def test_q9_it_holds_next_to_tagged_records(self) -> None:
        def script(message: str, test_id: str) -> Window:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_tokens(90))
            stray = TurnRecord(ts=1.0, usage=_tokens(90))
            return Window(turns=[turn, *([stray] if message == "c" else [])])

        tests = [*self._neighbours(), Case(trigger="c", expect=SAYS_OK)]

        first, _, third = _run(tests, _Failing(script, 1)).tests

        assert first.status is Status.ERROR
        assert third.status is Status.PASSED

    def test_q9_a_session_that_does_not_open_read_nothing(self) -> None:
        session = MagicMock()
        session.setup.side_effect = [{}, SessionError("no session")]
        agent = FakeAgent(_late_untagged(_tokens(90), _tokens(90)))

        first, second = _run(self._neighbours(), agent, session=session).tests

        assert first.status is Status.ERROR
        assert second.errors == ("no session",)

    def test_q9_a_trigger_that_does_not_resolve_read_nothing(self) -> None:
        tests = [_budgeted(self.LIMIT), Case(trigger="{{previous.nope}}", expect=SAYS_OK)]
        agent = FakeAgent(_late_untagged(_tokens(90), _tokens(90)))

        first, second = _run(tests, agent).tests

        assert first.status is Status.ERROR
        assert second.status is Status.ERROR
        assert len(agent.sent) == 1

    def test_q9_a_correlation_that_does_not_render_read_nothing(self) -> None:
        session = MagicMock()
        session.setup.side_effect = [{"id": "1"}, {}]
        agent = FakeAgent(_late_untagged(_tokens(90), _tokens(90)))
        correlate = {"conversation_id": "{{session.id}}"}

        summary = _run(self._neighbours(), agent, session=session, correlate=correlate)

        first, second = summary.tests
        assert first.status is Status.ERROR
        assert "agent.correlate" in second.errors[0]

    def test_q9_a_correlation_it_shares_read_nothing(self) -> None:
        session = MagicMock()
        session.setup.side_effect = [{"id": "1"}, {"id": "1"}]
        agent = FakeAgent(_late_untagged(_tokens(90), _tokens(90)))
        correlate = {"conversation_id": "{{session.id}}"}
        tests = [_budgeted(self.LIMIT), _budgeted(self.LIMIT)]

        first, second = _run(tests, agent, session=session, correlate=correlate).tests

        assert first.status is Status.ERROR
        assert "the same as an earlier test's" in second.errors[0]

    def test_q9_the_correlation_of_the_read_is_the_one_that_rendered(self) -> None:
        """A late `turn` tagged for the first test is its own, not one with no context."""
        session = MagicMock()
        session.setup.side_effect = [{"id": "1"}, {}]

        def script(message: str, test_id: str) -> tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"conversation_id": "1"}, usage=_tokens(90))
            return Window(turns=[turn]), Window(turns=[turn])

        correlate = {"conversation_id": "{{session.id}}"}

        summary = _run(self._neighbours(), FakeAgent(script), session=session, correlate=correlate)

        first = summary.tests[0]
        assert first.status is Status.FAILED
        assert first.totals is not None
        assert first.totals.input_tokens == 180

    def test_q9_the_sums_say_it_is_missing(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_tokens(90))
            return Window(turns=[turn]), Window(turns=[TurnRecord(ts=1.0, usage=_tokens(90))])

        summary = _run(self._neighbours(), _Failing(script, 1))

        first = summary.tests[0]
        assert first.totals is not None
        assert first.totals.input_tokens == 90
        assert first.totals.complete is False
        assert summary.totals is not None
        assert summary.totals.complete is False

    def test_q9_a_ceiling_counts_it_once(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_usd(0.1))
            return Window(turns=[turn]), Window(turns=[TurnRecord(ts=1.0, usage=_usd(9))])

        tests = [Case(trigger=name, expect=SAYS_OK) for name in "ab"]

        summary = _run(tests, _Failing(script, 1), max_cost=20)

        assert summary.untagged_cost == {"USD": 9.0}
        assert summary.stopped is None
        over = _run(tests, _Failing(script, 1), max_cost=9)
        assert over.stopped is not None
        assert "the run spent 9.1 USD (9.0 USD of it" in over.stopped

    @pytest.mark.parametrize("readers", [0, 1])
    def test_q9_it_holds_for_a_late_call(self, readers: int) -> None:
        """A forbidden call that lost its tag is left for the same test as usage that did."""
        sends = itertools.count()

        def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"test": test_id})
            call = replace(_call("refund", test_id), context={})
            now = Window(turns=[turn])
            return (now, Window(tools=[call])) if next(sends) == 0 else now

        guardrail = Case(trigger="a", expect=Expect(forbid=("refund",)))
        tests = [guardrail, Case(trigger="b", expect=SAYS_OK)]
        tests += [Case(trigger="c", expect=SAYS_OK)] * readers

        summary = _run(tests, _Failing(script, 1))

        assert summary.tests[0].status is Status.ERROR
        assert "refund" in summary.tests[0].errors[0]

    def test_q9_a_line_it_cannot_read_after_a_failed_send(self) -> None:
        """The line may be the late record of the test that read a window, not the failed one."""

        class Broken(_Failing):
            def collect(self, since: int) -> Window:
                if self.failed:
                    raise TraceFormatError("t.jsonl line 3: not valid JSON")
                return super().collect(since)

        agent = Broken(_late_untagged(_tokens(90), _tokens(90)), 1)

        first, _ = _run(self._neighbours(), agent).tests

        assert first.status is Status.ERROR
        assert "not valid JSON" in first.errors[0]

    def test_q9_with_nothing_read_at_all_the_run_still_counts_it(self, tmp_path: Path) -> None:
        """The agent wrote its `turn` and then answered 500: no test can answer for that."""

        class Refusing(_Traced):
            def send(self, message: str, *, values: Values) -> str:
                super().send(message, values=values)
                raise AgentResponseError("the agent answered 500: internal error")

        usage = {"input_tokens": 1, "output_tokens": 1, "cost": 9, "currency": "USD"}

        summary = _run([PLAIN], Refusing(tmp_path / "t.jsonl", _untagged(usage)), max_cost=20)

        assert summary.tests[0].status is Status.ERROR
        assert summary.untagged_cost == {"USD": 9.0}
        assert any("no test context" in warning for warning in summary.warnings)


BAD_LINE = "{not json\n"

# The lines of one message, and the lines written late, after its read.
Lines = tuple[list[str], list[str]]


def _turn_line(test_id: str, **usage: Any) -> str:
    record = {"v": 1, "ts": 1.0, "type": "turn", "context": {"test": test_id}, "usage": usage}
    return json.dumps(record) + "\n"


def _tool_line(fn: str, test_id: str) -> str:
    record = {"v": 1, "ts": 1.0, "type": "tool", "fn": fn, "context": {"test": test_id}}
    return json.dumps(record) + "\n"


class _Writing(FileTraceCollector):
    """An agent that writes real JSONL, a line that cannot be read included.

    ``script(ids)`` gives the lines of a message, ``ids`` being the test ids sent so far,
    its own last. The late ones are written after that message's read: at the next
    message, or before the read after the last test.
    """

    def __init__(self, path: Path, script: Callable[[list[str]], Lines]) -> None:
        super().__init__(path)
        self._script = script
        self._ids: list[str] = []
        self._late: list[str] = []
        self._sent = False
        self._append([json.dumps({"v": 1, "ts": 1.0, "type": "hello", "sdk": "t"}) + "\n"])

    def _append(self, lines: list[str]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write("".join(lines))

    def send(self, message: str, *, values: Values) -> str:
        self._append(self._late)
        self._ids.append(values.test_id)
        now, self._late = self._script(list(self._ids))
        self._append(now)
        self._sent = True
        return "ok"

    def collect(self, since: int) -> Window:
        if not self._sent:
            self._append(self._late)
            self._late = []
        self._sent = False
        return super().collect(since)


class TestUnreadableLine:
    """Q10: a line that cannot be read keeps the rest of its read. The test that read it
    is an error, and the one before it too if it passed and has a budget."""

    LIMIT = Budget(max_input_tokens=100)

    @staticmethod
    def _second_reads(late: Callable[[str], list[str]]) -> Callable[[list[str]], Lines]:
        """The first message spends 90 tokens and writes ``late(its id)`` after its read;
        the next ones spend 10."""

        def script(ids: list[str]) -> Lines:
            if len(ids) == 1:
                return [_turn_line(ids[0], input_tokens=90)], late(ids[0])
            return [_turn_line(ids[-1], input_tokens=10)], []

        return script

    def _bad_line(self) -> Callable[[list[str]], Lines]:
        return self._second_reads(lambda _: [BAD_LINE])

    def test_q10_late_usage_next_to_the_line_still_fails_its_run(self, tmp_path: Path) -> None:
        """180 tokens against 100, read in the next run's window with a line it cannot read."""
        script = self._second_reads(lambda first: [_turn_line(first, input_tokens=90), BAD_LINE])
        tests = [_budgeted(self.LIMIT, runs=2, min_pass=1)]

        summary = _run(tests, _Writing(tmp_path / "t.jsonl", script))

        (test,) = summary.tests
        first, second = test.phrasings[0].executions
        assert first.status is Status.FAILED
        assert "180 input tokens" in first.errors[0]
        assert second.status is Status.ERROR
        assert "not valid JSON" in second.errors[0]
        assert test.status is Status.ERROR
        assert summary.passed == 0
        assert test.totals is not None
        assert test.totals.input_tokens == 190

    def test_q10_the_line_may_be_the_usage_of_the_run_before(self, tmp_path: Path) -> None:
        tests = [_budgeted(self.LIMIT, runs=2, min_pass=1)]

        summary = _run(tests, _Writing(tmp_path / "t.jsonl", self._bad_line()))

        (test,) = summary.tests
        first, second = test.phrasings[0].executions
        assert first.status is Status.ERROR
        assert "may be this test's usage" in first.errors[0]
        assert "line 3: not valid JSON" in first.errors[0]
        assert second.status is Status.ERROR
        assert test.status is Status.ERROR
        assert summary.passed == 0

    def test_q10_a_limit_on_the_last_turn_counts_as_a_budget(self, tmp_path: Path) -> None:
        turns = [Turn(trigger="a", expect=Expect(budget=self.LIMIT))]
        tests = [Case(turns=turns), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, _Writing(tmp_path / "t.jsonl", self._bad_line()))

        first, second = summary.tests
        assert first.status is Status.ERROR
        assert "may be this test's usage" in first.errors[0]
        assert second.status is Status.ERROR

    def test_q10_a_test_with_no_budget_is_left_alone(self, tmp_path: Path) -> None:
        tests = [PLAIN, Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, _Writing(tmp_path / "t.jsonl", self._bad_line()))

        first, second = summary.tests
        assert first.status is Status.PASSED
        assert second.status is Status.ERROR
        assert "not valid JSON" in second.errors[0]

    def test_q10_a_test_that_failed_already_stays_failed(self, tmp_path: Path) -> None:
        tests = [_budgeted(Budget(max_input_tokens=50)), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, _Writing(tmp_path / "t.jsonl", self._bad_line()))

        first, _ = summary.tests
        assert first.status is Status.FAILED
        assert not any("may be this test's usage" in error for error in first.errors)

    def test_q10_a_late_call_next_to_the_line_still_fails_its_test(self, tmp_path: Path) -> None:
        def script(ids: list[str]) -> Lines:
            if len(ids) == 1:
                return [_turn_line(ids[0])], [_tool_line("refund", ids[0]), BAD_LINE]
            return [_turn_line(ids[-1])], []

        tests = [Case(trigger="a", expect=Expect(forbid=("refund",))), PLAIN]

        summary = _run(tests, _Writing(tmp_path / "t.jsonl", script))

        first, second = summary.tests
        assert first.status is Status.FAILED
        assert "refund" in first.errors[0]
        assert second.status is Status.ERROR

    def test_q10_the_usage_read_with_it_counts_in_the_sums_and_the_ceiling(
        self, tmp_path: Path
    ) -> None:
        def script(ids: list[str]) -> Lines:
            second = len(ids) == 2
            spent = _turn_line(ids[-1], input_tokens=1, cost=5 if second else 0.1, currency="USD")
            return [spent, *([BAD_LINE] if second else [])], []

        tests = [Case(trigger=name, expect=SAYS_OK) for name in "abc"]

        summary = _run(tests, _Writing(tmp_path / "t.jsonl", script), max_cost=1)

        assert [test.name for test in summary.tests] == ["a", "b"]
        second = summary.tests[1]
        assert second.status is Status.ERROR
        assert second.totals is not None
        assert second.totals.cost == {"USD": 5.0}
        assert summary.stopped is not None
        assert "5.1 USD" in summary.stopped

    def test_q10_the_read_after_the_last_test_keeps_what_it_can_read(self, tmp_path: Path) -> None:
        script = self._second_reads(lambda first: [_turn_line(first, input_tokens=90), BAD_LINE])

        (test,) = _run([_budgeted(self.LIMIT)], _Writing(tmp_path / "t.jsonl", script)).tests

        assert test.status is Status.FAILED
        assert "180 input tokens" in test.errors[0]

    def test_q10_after_the_last_test_the_line_errs_it_with_no_budget(self, tmp_path: Path) -> None:
        def script(ids: list[str]) -> Lines:
            return [_turn_line(ids[0])], [BAD_LINE]

        (test,) = _run([PLAIN], _Writing(tmp_path / "t.jsonl", script)).tests

        assert test.status is Status.ERROR
        assert "not valid JSON" in test.errors[0]


class TestUnreadableUsage:
    """Q2: a `usage` highbar cannot read, next to one it can, was spent too."""

    READABLE = {"input_tokens": 100, "output_tokens": 1, "cost": 0.1, "currency": "USD"}

    def test_q2_a_cost_that_cannot_be_read_leaves_the_budget_unverified(
        self, tmp_path: Path
    ) -> None:
        agent = _Traced(
            tmp_path / "t.jsonl",
            _tagged(self.READABLE),
            _tagged({"cost": "9.0", "currency": 5}),
        )

        (test,) = _run([_budgeted(Budget(max_cost=1))], agent).tests

        assert test.status is Status.ERROR
        assert "cannot check max_cost" in test.errors[0]
        assert test.totals is not None
        assert test.totals.complete is False
        assert test.totals.cost == {"USD": 0.1}

    def test_q2_tokens_that_cannot_be_read_leave_the_budget_unverified(
        self, tmp_path: Path
    ) -> None:
        agent = _Traced(
            tmp_path / "t.jsonl",
            _tagged({"input_tokens": 100}),
            _tagged({"input_tokens": "999999"}),
        )

        (test,) = _run([_budgeted(Budget(max_input_tokens=150))], agent).tests

        assert test.status is Status.ERROR
        assert "cannot check max_input_tokens" in test.errors[0]

    @pytest.mark.parametrize("value", ["lots", 12, {"prompt_tokens": 12}], ids=repr)
    def test_q2_any_usage_that_cannot_be_read_counts(self, tmp_path: Path, value: object) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _tagged(self.READABLE), _tagged(value))

        (test,) = _run([_budgeted(Budget(max_input_tokens=150))], agent).tests

        assert test.status is Status.ERROR

    @pytest.mark.parametrize("fields", [{}, {"usage": None}, {"usage": {}}], ids=repr)
    def test_q2_a_record_that_says_nothing_is_still_ignored(
        self, tmp_path: Path, fields: dict[str, Any]
    ) -> None:
        """The middleware's `turn`, next to the `hb.turn()` that has the usage."""
        agent = _Traced(tmp_path / "t.jsonl", _tagged(self.READABLE), fields)

        (test,) = _run([_budgeted(Budget(max_cost=1))], agent).tests

        assert test.status is Status.PASSED
        assert test.totals is not None
        assert test.totals.complete is True

    def test_q2_alone_it_reads_as_no_usage(self, tmp_path: Path) -> None:
        """AC1: a writer with a `usage` of its own works as before, and owes no warning."""
        agent = _Traced(tmp_path / "t.jsonl", _tagged({"prompt_tokens": 12}))

        summary = _run([Case(trigger="t", expect=SAYS_OK)], agent)

        assert summary.totals is None
        assert summary.warnings == ()

    def test_q2_alone_it_cannot_pass_a_budget(self, tmp_path: Path) -> None:
        agent = _Traced(tmp_path / "t.jsonl", _tagged({"input_tokens": "1"}))

        (test,) = _run([_budgeted(Budget(max_input_tokens=150))], agent).tests

        assert test.status is Status.ERROR
        assert "no usage" in test.errors[0]


class TestKnownSpend:
    """Q3: what one record knows counts, whatever the record next to it leaves out."""

    def test_q3_known_tokens_over_a_limit_fail_it(self) -> None:
        agent = _spending((Usage(input_tokens=999_999), Usage(cost=1.0, currency="USD")))

        (test,) = _run([_budgeted(Budget(max_input_tokens=150))], agent).tests

        assert test.status is Status.FAILED
        assert test.errors == ("the execution used 999999 input tokens, over max_input_tokens 150",)

    def test_q3_a_known_cost_over_a_limit_fails_it(self) -> None:
        agent = _spending((_usd(5.0), Usage(input_tokens=1)))

        (test,) = _run([_budgeted(Budget(max_cost=1))], agent).tests

        assert test.status is Status.FAILED
        assert test.totals is not None
        assert test.totals.cost == {"USD": 5.0}
        assert test.totals.complete is False

    def test_q3_a_limit_on_a_turn_holds_the_same(self) -> None:
        turns = [Turn(trigger="a", expect=Expect(budget=Budget(max_cost=1)))]
        agent = _spending((_usd(5.0), Usage(input_tokens=1)))

        (test,) = _run([Case(turns=turns)], agent).tests

        assert test.status is Status.FAILED

    def test_q3_the_ceiling_sees_a_known_cost(self) -> None:
        agent = _spending((_usd(5.0), Usage(input_tokens=1)))
        tests = [Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, agent, max_cost=1)

        assert len(agent.sent) == 1
        assert summary.stopped is not None
        assert "5.0 USD" in summary.stopped

    def test_q3_its_warning_does_not_deny_a_cost_it_counted(self) -> None:
        agent = _spending((_usd(0.1), Usage(input_tokens=1)))

        summary = _run([Case(trigger="a", expect=SAYS_OK)], agent, max_cost=5)

        (warning,) = summary.warnings
        assert "1 of 1 turns are missing a cost" in warning

    def test_q3_two_currencies_in_one_message_lose_no_number(self) -> None:
        agent = _spending((_usd(5.0), Usage(cost=7.0, currency="EUR")))

        (test,) = _run([Case(trigger="t", expect=SAYS_OK)], agent).tests

        (turn,) = test.phrasings[0].executions[0].turns
        assert turn.usage is not None
        assert turn.usage.cost is None
        assert test.totals is not None
        assert test.totals.cost == {"USD": 5.0, "EUR": 7.0}

    def test_q3_the_ceiling_holds_each_of_them(self) -> None:
        agent = _spending((_usd(0.1), Usage(cost=7.0, currency="EUR")))

        summary = _run([Case(trigger="t", expect=SAYS_OK)], agent, max_cost=1)

        assert summary.stopped is not None
        assert "7.0 EUR" in summary.stopped


class TestLateUsage:
    """Q4: a tagged `turn` read after its window, judged as a late call is."""

    LIMIT = Budget(max_input_tokens=150)

    def test_q4_it_fails_a_test_it_takes_over_its_budget(self) -> None:
        tests = [_budgeted(self.LIMIT), Case(trigger="b", expect=SAYS_OK)]
        reporter = MagicMock()

        summary = _run(tests, FakeAgent(_late(_tokens(100), _tokens(999_999))), reporter=reporter)

        first, second = summary.tests
        assert first.status is Status.FAILED
        assert "after the test's window closed" in first.errors[0]
        assert "1000099 input tokens" in first.errors[0]
        assert "agent.settle_time" in first.errors[0]
        assert first.totals is not None
        assert first.totals.input_tokens == 1000099
        assert second.status is Status.PASSED
        assert any("now fails" in warning for warning in summary.warnings)

    def test_q4_it_can_land_after_the_last_test(self) -> None:
        agent = FakeAgent(_late(_tokens(100), _tokens(999_999)))

        (test,) = _run([_budgeted(self.LIMIT)], agent).tests

        assert test.status is Status.FAILED

    def test_q4_it_holds_for_a_limit_on_the_last_turn(self) -> None:
        turns = [Turn(trigger="a", expect=Expect(budget=self.LIMIT))]
        agent = FakeAgent(_late(_tokens(100), _tokens(999_999)))

        (test,) = _run([Case(turns=turns)], agent).tests

        assert test.status is Status.FAILED

    def test_q4_within_the_budget_it_only_adds_to_the_sum(self) -> None:
        agent = FakeAgent(_late(_tokens(100), _tokens(10)))

        (test,) = _run([_budgeted(self.LIMIT)], agent).tests

        assert test.status is Status.PASSED
        assert test.totals is not None
        assert test.totals.input_tokens == 110

    def test_q4_a_late_record_with_nothing_on_the_limit_leaves_it_unverified(self) -> None:
        agent = FakeAgent(_late(_tokens(100), Usage(cost=1.0, currency="USD")))

        (test,) = _run([_budgeted(self.LIMIT)], agent).tests

        assert test.status is Status.ERROR
        assert "after the test's window closed" in test.errors[0]

    def test_q4_an_unreadable_late_record_leaves_it_unverified(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            context = {"test": test_id}
            turn = TurnRecord(ts=1.0, context=context, usage=_tokens(100))
            unread = TurnRecord(ts=1.0, context=context, usage_unreadable=True)
            return Window(turns=[turn]), Window(turns=[unread])

        (test,) = _run([_budgeted(self.LIMIT)], FakeAgent(script)).tests

        assert test.status is Status.ERROR

    def test_q4_a_late_turn_without_usage_changes_nothing(self) -> None:
        agent = FakeAgent(_late(_tokens(100), None))

        (test,) = _run([_budgeted(self.LIMIT)], agent).tests

        assert test.status is Status.PASSED

    def test_q4_it_fails_only_its_own_run(self) -> None:
        test = _budgeted(self.LIMIT, runs=3, min_pass=2)

        (outcome,) = _run([test], FakeAgent(_late(_tokens(100), _tokens(999_999)))).tests

        assert outcome.status is Status.PASSED
        assert outcome.counts == Counts(passed=2, failed=1, error=0)

    def test_q4_a_test_that_failed_already_stays_failed(self) -> None:
        test = Case(trigger="t", expect=Expect(response=FAILS.response, budget=self.LIMIT))

        (outcome,) = _run([test], FakeAgent(_late(_tokens(100), _tokens(999_999)))).tests

        assert outcome.status is Status.FAILED
        assert not any("window closed" in error for error in outcome.errors)

    def test_q4_the_ceiling_counts_it_in_the_next_test(self) -> None:
        tests = [Case(trigger=name, expect=SAYS_OK) for name in "abc"]
        agent = FakeAgent(_late(_usd(0.1), _usd(5.0)))

        summary = _run(tests, agent, max_cost=1)

        assert [message for message, _ in agent.sent] == ["a", "b"]
        assert summary.stopped is not None

    def test_q4_the_ceiling_counts_it_after_the_last_test(self) -> None:
        """The run read that record, so one that ended over its ceiling says so (exit 3)."""
        agent = FakeAgent(_late(_usd(0.1), _usd(5.0)))

        summary = _run([_budgeted(Budget(max_cost=100))], agent, max_cost=1)

        assert summary.stopped is not None
        assert "5.1 USD" in summary.stopped

    def test_q7_the_ceiling_counts_it_when_no_test_observes(self) -> None:
        """A test that only checks the reply spent it too, and the run still ends over."""
        agent = FakeAgent(_late(_usd(0.1), _usd(5.0)))

        summary = _run([PLAIN], agent, max_cost=1)

        assert summary.stopped is not None
        assert "5.1 USD" in summary.stopped

    def test_q7_the_sums_count_it_when_no_test_observes(self) -> None:
        agent = FakeAgent(_late(_usd(0.1), _usd(5.0)))

        summary = _run([PLAIN], agent)

        assert summary.totals is not None
        assert summary.totals.cost == {"USD": 5.1}

    def test_q7_the_ceiling_counts_usage_with_no_context_too(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_usd(0.1))
            return Window(turns=[turn]), Window(turns=[TurnRecord(ts=1.0, usage=_usd(5.0))])

        summary = _run([PLAIN], FakeAgent(script), max_cost=1)

        assert summary.stopped is not None
        assert "5.1 USD (5.0 USD of it" in summary.stopped

    def test_q7_a_ceiling_asks_no_hello_of_an_agent_no_test_observes(self) -> None:
        agent = FakeAgent(_late(_usd(0.1), _usd(5.0)), hello=False)

        summary = _run([PLAIN], agent, max_cost=1)

        assert summary.stopped is not None


class TestCeiling:
    @staticmethod
    def _tests(count: int) -> list[Case]:
        return [Case(trigger=f"m{index}", expect=SAYS_OK) for index in range(count)]

    def test_ac12_the_run_stops_at_the_end_of_the_test_that_crossed_it(self) -> None:
        agent = _reporting(_usd(0.01))

        summary = _run(self._tests(3), agent, max_cost=0.015)

        assert [message for message, _ in agent.sent] == ["m0", "m1"]
        assert [test.name for test in summary.tests] == ["m0", "m1"]
        assert summary.total == 2
        assert summary.stopped is not None
        assert "--max-cost 0.015" in summary.stopped
        assert "0.02 USD" in summary.stopped
        assert "'m1'" in summary.stopped

    def test_ac12_the_agent_is_stopped_and_the_results_kept(self) -> None:
        agent = _reporting(_usd(0.01))
        runner = _runner(agent, max_cost=0.005)
        spec = Spec(
            agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
            suites=[Suite(name="S", tests=self._tests(2))],
        )

        summary = runner.run(spec)

        runner._adapter.stop.assert_called_once()
        assert summary.passed == 1

    def test_ac12_equal_to_the_ceiling_goes_on(self) -> None:
        agent = _reporting(_usd(0.01))

        summary = _run(self._tests(3), agent, max_cost=0.03)

        assert summary.stopped is None
        assert len(agent.sent) == 3

    def test_ac12_the_test_in_progress_runs_to_its_end(self) -> None:
        agent = _reporting(_usd(0.01))
        tests = [Case(trigger="a", expect=SAYS_OK, runs=3), Case(trigger="b", expect=SAYS_OK)]

        summary = _run(tests, agent, max_cost=0.005)

        assert [message for message, _ in agent.sent] == ["a", "a", "a"]
        assert summary.tests[0].counts == Counts(passed=3, failed=0, error=0)

    def test_ac12_the_next_suite_does_not_start(self) -> None:
        reporter = MagicMock()
        spec = Spec(
            agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
            suites=[
                Suite(name="A", tests=self._tests(1)),
                Suite(name="B", tests=self._tests(1)),
            ],
        )

        _runner(_reporting(_usd(0.01)), reporter=reporter, max_cost=0.005).run(spec)

        assert reporter.on_suite_start.call_count == 1

    def test_ac12_earlier_specs_count(self) -> None:
        agent = _reporting(_usd(0.01))

        summary = _run(self._tests(2), agent, max_cost=0.025, spent={"USD": 0.02})

        assert len(agent.sent) == 1
        assert summary.stopped is not None
        assert "0.03 USD" in summary.stopped

    def test_each_currency_is_held_to_it_on_its_own(self) -> None:
        agent = _reporting(_usd(0.01), Usage(cost=0.02, currency="EUR"))

        summary = _run(self._tests(3), agent, max_cost=0.015)

        assert len(agent.sent) == 2
        assert summary.stopped is not None
        assert "0.02 EUR" in summary.stopped

    def test_a_test_that_stopped_the_run_can_still_fail_late(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            turn = TurnRecord(ts=1.0, context={"test": test_id}, usage=_usd(1.0))
            return Window(turns=[turn]), Window(tools=[_call("refund", test_id)])

        test = Case(trigger="t", expect=Expect(forbid=("refund",)))

        summary = _run([test, test], FakeAgent(script), max_cost=0.5)

        assert summary.tests[0].status is Status.FAILED
        assert summary.stopped is not None

    def test_without_a_ceiling_nothing_stops(self) -> None:
        agent = _reporting(_usd(100.0))

        assert _run(self._tests(2), agent).stopped is None
        assert len(agent.sent) == 2

    def test_a_ceiling_that_cannot_see_a_cost_says_so(self) -> None:
        agent = _reporting(_usd(0.01), None)

        summary = _run(self._tests(2), agent, max_cost=5)

        assert summary.stopped is None
        (warning,) = summary.warnings
        assert "--max-cost 5" in warning
        assert "1 of 2 turns" in warning

    def test_a_ceiling_that_sees_every_cost_says_nothing(self) -> None:
        assert _run(self._tests(2), _reporting(_usd(0.01)), max_cost=5).warnings == ()
