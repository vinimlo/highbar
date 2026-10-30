"""A run keeps every execution, in its final state (spec D13, D14, D18)."""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
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
    Window,
)
from highbar.domain.models import (
    Test as Case,
)
from highbar.engine.placeholders import Values
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.session import SessionError
from tests.unit.engine.fakes import FakeAgent, Script, observed

GUARDRAIL = Expect(function_calls=[])
SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))
TOKEN = "s3cr3t-token-value"


def _tool(fn: str, args: dict[str, Any] | None = None, **context: str) -> Trace:
    return Trace(
        fn=fn, args=args or {}, result="raw", started_at=1.0, ended_at=1.25, context=context
    )


def _turn(**context: str) -> TurnRecord:
    return TurnRecord(ts=1.0, context=context)


class Replying(FakeAgent):
    """A FakeAgent that answers from a list, and raises what the list holds as an exception."""

    def __init__(self, replies: list[str | BaseException], script: Script = observed) -> None:
        super().__init__(script)
        self._replies = iter(replies)

    def send(self, message: str, *, values: Values) -> str:
        super().send(message, values=values)
        reply = next(self._replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _runner(
    agent: FakeAgent,
    *,
    reporter: MagicMock | None = None,
    secrets: Mapping[str, str] | None = None,
    correlate: Mapping[str, Any] | None = None,
    session: Any = None,
    variations: Any = None,
) -> Runner:
    return Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=0,
        secrets=secrets,
        correlate=correlate,
        session=session,
        variations=variations,
    )


def _spec(tests: list[Case]) -> Spec:
    return Spec(
        agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
        suites=[Suite(name="S", tests=tests)],
    )


def _run(tests: list[Case], agent: FakeAgent, **options: Any) -> RunSummary:
    return _runner(agent, **options).run(_spec(tests))


class TestLateCalls:
    """on_test_result goes out before a late call can change the verdict (D13)."""

    @staticmethod
    def _script(message: str, test_id: str) -> Window | tuple[Window, Window]:
        if message == "close my account":
            return Window(turns=[_turn(test=test_id)]), Window(
                tools=[_tool("delete_account", test=test_id)]
            )
        return Window(turns=[_turn(test=test_id)])

    def test_j1_late_call_fails_a_reported_test_in_the_final_result(self) -> None:
        reporter = MagicMock()

        summary = _run(
            [Case(trigger="close my account", expect=GUARDRAIL), Case(trigger="b", expect=SAYS_OK)],
            FakeAgent(self._script),
            reporter=reporter,
        )

        # What makes this the trap: the event said passed, and the result must not.
        assert reporter.on_test_result.call_args_list[0][0][1].status is Status.PASSED
        first, second = summary.tests
        assert first.status is Status.FAILED
        assert first.counts == Counts(passed=0, failed=1, error=0)
        assert "delete_account" in first.errors[0]
        (phrasing,) = first.phrasings
        assert phrasing.status is Status.FAILED
        (execution,) = phrasing.executions
        assert execution.status is Status.FAILED
        assert "delete_account" in execution.errors[0]
        (tool,) = execution.turns[0].tools
        assert (tool.name, tool.late) == ("delete_account", True)
        assert second.status is Status.PASSED

    def test_j1_the_last_tests_late_call_counts_too(self) -> None:
        summary = _run(
            [Case(trigger="close my account", expect=GUARDRAIL)], FakeAgent(self._script)
        )

        assert summary.tests[0].status is Status.FAILED
        assert summary.tests[0].phrasings[0].executions[0].status is Status.FAILED

    def test_j1_summary_totals_are_the_sum_of_the_tests(self) -> None:
        summary = _run(
            [Case(trigger="close my account", expect=GUARDRAIL), Case(trigger="b", expect=SAYS_OK)],
            FakeAgent(self._script),
        )

        assert (summary.total, summary.passed, summary.failed, summary.errored) == (2, 1, 1, 0)
        assert [test.status for test in summary.tests] == [Status.FAILED, Status.PASSED]

    def test_a_late_call_that_keeps_the_verdict_is_still_listed(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            return Window(turns=[_turn(test=test_id)]), Window(tools=[_tool("log", test=test_id)])

        summary = _run([Case(trigger="a", expect=Expect(forbid=("other",)))], FakeAgent(script))

        assert summary.tests[0].status is Status.PASSED
        (tool,) = summary.tests[0].phrasings[0].executions[0].turns[0].tools
        assert (tool.name, tool.late) == ("log", True)

    def test_j1_late_failure_of_one_run_changes_only_that_run(self) -> None:
        sends = itertools.count()

        def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
            now = Window(turns=[_turn(test=test_id)])
            return (now, Window(tools=[_tool("x", test=test_id)])) if next(sends) == 0 else now

        summary = _run([Case(trigger="a", expect=GUARDRAIL, runs=2, min_pass=1)], FakeAgent(script))

        (test,) = summary.tests
        assert test.status is Status.PASSED
        assert test.counts == Counts(passed=1, failed=1, error=0)
        assert [e.status for e in test.phrasings[0].executions] == [Status.FAILED, Status.PASSED]


class TestThreeStates:
    def test_j2_errors_stay_apart_in_the_counts(self) -> None:
        agent = Replying(["ok", "no", OSError("connection reset")])

        summary = _run([Case(trigger="t", expect=SAYS_OK, runs=3, min_pass=2)], agent)

        (test,) = summary.tests
        assert test.counts == Counts(passed=1, failed=1, error=1)
        # 1 passed and 1 unverified could have reached 2: unverified, not failed (D2).
        assert test.status is Status.ERROR
        (phrasing,) = test.phrasings
        assert phrasing.counts == test.counts
        assert [e.status for e in phrasing.executions] == [
            Status.PASSED,
            Status.FAILED,
            Status.ERROR,
        ]


class TestSecrets:
    def test_j3_nothing_from_a_var_reaches_the_result(self) -> None:
        def script(message: str, test_id: str) -> Window:
            args = {"nested": {"key": [TOKEN]}, TOKEN: 1, "pin": 987654}
            return Window(tools=[_tool("lookup", args, conv=TOKEN)], turns=[_turn(conv=TOKEN)])

        session = MagicMock()
        session.setup.return_value = {"conv": TOKEN}
        session.teardown.side_effect = SessionError(f"could not close {TOKEN}")

        summary = _run(
            [Case(trigger="t", expect=Expect(response=ResponseExpect(matcher={"contains": "no"})))],
            Replying([f"your token is {TOKEN}"], script),
            secrets={TOKEN: "AGENT_TOKEN", "987654": "PIN"},
            correlate={"conv": "{{session.conv}}"},
            session=session,
        )

        assert summary.tests[0].status is Status.FAILED
        assert TOKEN not in repr(summary)
        assert "987654" not in repr(summary)
        (turn,) = summary.tests[0].phrasings[0].executions[0].turns
        assert turn.response == "your token is ${AGENT_TOKEN}"
        assert turn.correlate == {"conv": "${AGENT_TOKEN}"}
        assert turn.tools[0].args == {
            "nested": {"key": ["${AGENT_TOKEN}"]},
            "${AGENT_TOKEN}": 1,
            "pin": "${PIN}",
        }
        assert summary.warnings == ("t: session teardown failed: could not close ${AGENT_TOKEN}",)

    def test_i3_an_injected_reporter_gets_the_same_clean_data(self) -> None:
        def script(message: str, test_id: str) -> Window:
            tool = Trace(
                fn="lookup",
                args={"nested": {"key": [TOKEN]}},
                result={"echo": TOKEN},
                started_at=1.0,
                ended_at=1.25,
                error=f"refused {TOKEN}",
                context={"test": test_id, "conv": TOKEN},
            )
            return Window(tools=[tool], turns=[_turn(test=test_id)])

        reporter = MagicMock()

        _run(
            [Case(trigger="t", expect=SAYS_OK)],
            FakeAgent(script),
            reporter=reporter,
            secrets={TOKEN: "AGENT_TOKEN"},
        )

        (_, result) = reporter.on_test_result.call_args[0]
        (trace,) = result.traces
        assert TOKEN not in repr(result)
        assert trace.args == {"nested": {"key": ["${AGENT_TOKEN}"]}}
        assert trace.result == {"echo": "${AGENT_TOKEN}"}
        assert trace.error == "refused ${AGENT_TOKEN}"
        assert trace.context["conv"] == "${AGENT_TOKEN}"

    def test_i5_a_value_the_message_cuts_is_still_masked(self) -> None:
        reporter = MagicMock()

        summary = _run(
            [Case(trigger="t", expect=Expect(response=ResponseExpect({"contains": "absent"})))],
            Replying(["x" * 290 + TOKEN]),
            reporter=reporter,
            secrets={TOKEN: "AGENT_TOKEN"},
        )

        (_, result) = reporter.on_test_result.call_args[0]
        (execution,) = summary.tests[0].phrasings[0].executions
        for message in (result.errors[0], summary.tests[0].errors[0], execution.errors[0]):
            assert "s3cr3t" not in message
            assert "${AGENT_TOKEN}…" in message


class TestShape:
    def test_a_test_carries_its_identity(self) -> None:
        test = Case(trigger="hello", expect=SAYS_OK, runs=4, min_pass=0.5, id="greeting", name="Hi")

        (outcome,) = _run([test], FakeAgent()).tests

        assert (outcome.id, outcome.suite, outcome.name) == ("greeting", "S", "Hi")
        assert outcome.definition_sha256 == test.definition_sha256
        assert (outcome.runs, outcome.min_pass) == (4, 2)

    def test_an_execution_carries_what_happened(self) -> None:
        agent = FakeAgent()

        (outcome,) = _run([Case(trigger="hello", expect=SAYS_OK)], agent).tests

        (execution,) = outcome.phrasings[0].executions
        assert execution.execution_id == agent.test_ids[0]
        assert execution.run == 1
        assert execution.started_at.endswith("Z")
        assert execution.duration_ms >= 0
        (turn,) = execution.turns
        assert (turn.trigger, turn.response) == ("hello", "ok")
        assert turn.response_ms is not None
        assert turn.correlate == {"test": agent.test_ids[0]}

    def test_a_tool_has_no_result_nor_context(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(tools=[_tool("lookup", {"q": "x"}, test=test_id)])

        (outcome,) = _run([Case(trigger="t", expect=SAYS_OK)], FakeAgent(script)).tests

        (tool,) = outcome.phrasings[0].executions[0].turns[0].tools
        assert (tool.name, tool.args, tool.error, tool.late) == ("lookup", {"q": "x"}, None, False)
        assert tool.duration_ms == pytest.approx(250)
        assert not hasattr(tool, "result")

    def test_j11_multi_turn_stops_at_the_turn_that_failed(self) -> None:
        turns = [
            Turn(trigger="first", expect=Expect(response=ResponseExpect({"contains": "absent"}))),
            Turn(trigger="second", expect=SAYS_OK),
        ]

        (outcome,) = _run([Case(turns=turns)], FakeAgent()).tests

        (phrasing,) = outcome.phrasings
        assert (phrasing.kind, phrasing.text) == ("conversation", None)
        (execution,) = phrasing.executions
        assert [turn.trigger for turn in execution.turns] == ["first"]

    def test_j11_variations_are_phrasings(self) -> None:
        generator = MagicMock()
        generator.generate.return_value = ["v1", "v2"]

        (outcome,) = _run(
            [Case(trigger="t", expect=SAYS_OK, variations=2, runs=2)],
            FakeAgent(),
            variations=generator,
        ).tests

        assert [(p.kind, p.text) for p in outcome.phrasings] == [
            ("trigger", "t"),
            ("variation", "v1"),
            ("variation", "v2"),
        ]
        assert [e.run for e in outcome.phrasings[1].executions] == [1, 2]
        assert outcome.counts == Counts(passed=6, failed=0, error=0)

    def test_a_test_stopped_before_running_has_no_phrasings(self) -> None:
        generator = MagicMock()
        generator.generate.return_value = []

        (outcome,) = _run(
            [Case(trigger="t", expect=SAYS_OK, variations=2)], FakeAgent(), variations=generator
        ).tests

        assert outcome.status is Status.ERROR
        assert "no variations available" in outcome.errors[0]
        assert outcome.phrasings == ()
        assert outcome.counts == Counts()

    def test_j15_each_turn_keeps_the_correlate_it_used(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(turns=[_turn(conv="c1")])

        session = MagicMock()
        session.setup.return_value = {"conv": "c1"}
        turns = [Turn(trigger="a", expect=SAYS_OK), Turn(trigger="b", expect=SAYS_OK)]

        (outcome,) = _run(
            [Case(turns=turns)],
            FakeAgent(script),
            correlate={"conv": "{{session.conv}}"},
            session=session,
        ).tests

        (execution,) = outcome.phrasings[0].executions
        assert [turn.correlate for turn in execution.turns] == [{"conv": "c1"}, {"conv": "c1"}]

    def test_failed_send_has_no_response(self) -> None:
        (outcome,) = _run(
            [Case(trigger="t", expect=SAYS_OK)], Replying([OSError("connection reset")])
        ).tests

        (turn,) = outcome.phrasings[0].executions[0].turns
        assert (turn.trigger, turn.response, turn.response_ms) == ("t", None, None)

    def test_a_template_that_cannot_resolve_keeps_the_raw_trigger(self) -> None:
        trigger = "{{previous.lookup.result}}"

        (outcome,) = _run([Case(trigger=trigger, expect=SAYS_OK)], FakeAgent()).tests

        assert outcome.status is Status.ERROR
        (turn,) = outcome.phrasings[0].executions[0].turns
        assert (turn.trigger, turn.response) == (trigger, None)

    def test_a_session_that_does_not_open_has_no_turns(self) -> None:
        session = MagicMock()
        session.setup.side_effect = SessionError("no session")

        (outcome,) = _run([Case(trigger="t", expect=SAYS_OK)], FakeAgent(), session=session).tests

        (execution,) = outcome.phrasings[0].executions
        assert execution.status is Status.ERROR
        assert execution.errors == ("no session",)
        assert execution.turns == ()


class TestSnapshot:
    def test_snapshot_mid_run_holds_only_finished_tests(self) -> None:
        agent = Replying(["ok", KeyboardInterrupt()])
        runner = _runner(agent)

        with pytest.raises(KeyboardInterrupt):
            runner.run(
                _spec([Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)])
            )

        summary = runner.snapshot()
        assert [test.id for test in summary.tests] == ["S/a"]
        assert (summary.total, summary.passed) == (1, 1)

    @staticmethod
    def _late_first(message: str, test_id: str) -> Window | tuple[Window, Window]:
        now = Window(turns=[_turn(test=test_id)])
        if message == "close my account":
            return now, Window(tools=[_tool("delete_account", test=test_id)])
        return now

    def test_c1_a_late_call_read_before_the_interrupt_counts(self) -> None:
        """The next test's read found it, and that test never ends to apply it."""
        reporter = MagicMock()
        runner = _runner(
            Replying(["ok", "ok", KeyboardInterrupt()], self._late_first), reporter=reporter
        )

        with pytest.raises(KeyboardInterrupt):
            runner.run(
                _spec(
                    [
                        Case(trigger="close my account", expect=GUARDRAIL),
                        Case(trigger="b", expect=GUARDRAIL, runs=3),
                    ]
                )
            )

        summary = runner.snapshot()
        (test,) = summary.tests
        assert test.status is Status.FAILED
        assert test.counts == Counts(passed=0, failed=1, error=0)
        assert "delete_account" in test.errors[0]
        assert (summary.passed, summary.failed) == (0, 1)
        # The terminal says it too: a ✓ with no warning would contradict the file.
        (warning,) = [c.args[0] for c in reporter.on_warning.call_args_list]
        assert "now fails" in warning

    def test_c1_the_interrupt_can_land_while_the_next_test_is_shown(self) -> None:
        reporter = MagicMock()
        reporter.on_test_result.side_effect = [None, KeyboardInterrupt()]
        runner = _runner(FakeAgent(self._late_first), reporter=reporter)

        with pytest.raises(KeyboardInterrupt):
            runner.run(
                _spec(
                    [
                        Case(trigger="close my account", expect=GUARDRAIL),
                        Case(trigger="b", expect=GUARDRAIL),
                    ]
                )
            )

        first, second = runner.snapshot().tests
        assert (first.status, second.status) == (Status.FAILED, Status.PASSED)

    def test_c1_a_late_call_of_the_test_in_progress_stays_out(self) -> None:
        runner = _runner(Replying(["ok", "ok", KeyboardInterrupt()], self._late_first))

        with pytest.raises(KeyboardInterrupt):
            runner.run(_spec([Case(trigger="close my account", expect=GUARDRAIL, runs=3)]))

        assert runner.snapshot().tests == ()

    def test_snapshot_before_the_agent_starts_is_empty(self) -> None:
        agent = FakeAgent()
        runner = _runner(agent)
        runner._adapter.start.side_effect = RuntimeError("no agent")  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

        with pytest.raises(RuntimeError):
            runner.run(_spec([Case(trigger="a", expect=SAYS_OK)]))

        assert runner.snapshot().tests == ()
