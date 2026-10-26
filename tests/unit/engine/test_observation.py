"""Correlation and proof of observation (spec D4; review finding A4)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from highbar.adapters.file_collector import TraceFormatError
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
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
    Window,
)
from highbar.domain.models import (
    Test as Case,
)
from highbar.engine import observation
from highbar.engine.observation import AgentNotObservedError, classify, same_value
from highbar.engine.runner import TestRunner as Runner
from tests.unit.engine.fakes import FakeAgent

GUARDRAIL = Expect(function_calls=[])
CALLS_LOOKUP = Expect(function_calls=[FunctionCallExpect(name="lookup")])
CALLS_CANCEL = Expect(function_calls=[FunctionCallExpect(name="cancel_order")])
SAYS_OK = Expect(response=ResponseExpect(contains="ok"))


def _tool(fn: str, **context: str) -> Trace:
    return Trace(fn=fn, args={}, result=None, started_at=1.0, ended_at=1.1, context=context)


def _turn(**context: str) -> TurnRecord:
    return TurnRecord(ts=1.0, context=context)


def _run(
    tests: list[Case], agent: FakeAgent, *, managed: bool = True
) -> tuple[list[tuple[str, object]], MagicMock, MagicMock]:
    adapter, reporter = MagicMock(), MagicMock()
    runner = Runner(
        adapter=adapter,
        client=agent,
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter,
        settle_time=0,
        managed=managed,
    )
    spec = Spec(
        agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
        suites=[Suite(name="S", tests=tests)],
    )
    runner.run(spec)
    results = [c[0] for c in reporter.on_test_result.call_args_list]
    return results, adapter, reporter


class TestClassify:
    def test_records_are_mine_other_or_unattributed(self) -> None:
        window = Window(
            tools=[
                _tool("mine", test="a", conversation_id="7"),
                _tool("other", test="b"),
                _tool("untagged"),
                _tool("no-test-key", conversation_id=7),
                _tool("test-is-null", test=None),
            ],
            turns=[_turn(test="a"), _turn(test="b")],
        )

        classified = classify(window, {"test": "a"})

        assert classified.correlated
        assert [t.fn for t in classified.mine.tools] == ["mine"]
        assert [t.fn for t in classified.other.tools] == ["other"]
        # No correlation key (or only null): nothing says whose they are.
        assert [t.fn for t in classified.unattributed.tools] == [
            "untagged",
            "no-test-key",
            "test-is-null",
        ]
        assert classified.mine.turns == [_turn(test="a")]

    def test_a_record_that_agrees_on_some_keys_and_lacks_others_is_unattributed(self) -> None:
        correlate = {"test": "a", "conversation_id": 7}

        classified = classify(
            Window(tools=[_tool("partial", conversation_id="7"), _tool("off", conversation_id=8)]),
            correlate,
        )

        assert [t.fn for t in classified.unattributed.tools] == ["partial"]
        assert [t.fn for t in classified.other.tools] == ["off"]

    def test_a_window_without_correlation_keys_is_not_correlated(self) -> None:
        classified = classify(
            Window(tools=[_tool("a"), _tool("b", conversation_id=1)]), {"test": "x"}
        )

        assert not classified.correlated

    def test_int_and_string_ids_correlate(self) -> None:
        """The session says 42, the emitter wrote "42" (or a header made it text)."""
        assert same_value(42, "42")
        assert same_value("42", 42)
        assert same_value(7, 7)
        assert not same_value(True, 1)
        assert not same_value(True, "True")
        assert not same_value(None, "None")
        assert not same_value({"a": 1}, "{'a': 1}")


class TestTestId:
    def test_each_execution_sends_its_own_test_id(self) -> None:
        agent = FakeAgent()
        multi = Case(
            turns=[
                Turn(trigger="one", expect=GUARDRAIL),
                Turn(trigger="two", expect=GUARDRAIL),
                Turn(trigger="three", expect=GUARDRAIL),
            ]
        )

        _run(
            [Case(trigger="a", expect=GUARDRAIL), Case(trigger="b", expect=GUARDRAIL), multi],
            agent,
        )

        first, second, *turns = agent.test_ids
        assert first != second
        assert len(set(turns)) == 1
        assert turns[0] not in (first, second)


class TestCorrelation:
    def test_a4_late_call_does_not_land_in_the_next_test(self) -> None:
        """A tool that finished after the first test's reply used to fail the next one."""
        agent: FakeAgent

        def script(message: str, test_id: str) -> Window:
            if message == "cancel my order":
                return Window(turns=[_turn(test=test_id)])
            previous_id = agent.test_ids[0]
            return Window(
                tools=[_tool("delete_account", test=previous_id)],
                turns=[_turn(test=test_id)],
            )

        agent = FakeAgent(script)
        results, _, _ = _run(
            [
                Case(
                    trigger="cancel my order", expect=Expect(response=ResponseExpect(contains="ok"))
                ),
                Case(trigger="tell me a joke", expect=GUARDRAIL),
            ],
            agent,
        )

        assert [result.status for _, result in results] == [Status.PASSED, Status.PASSED]

    def test_unattributed_call_never_lets_a_guardrail_pass(self) -> None:
        """A tool run in a thread the agent starts loses the context. Dropping it from a
        correlated window would let `function_calls: []` pass with the call made; whose
        it is cannot be told, so the test is unverified."""

        def script(message: str, test_id: str) -> Window:
            return Window(tools=[_tool("delete_account")], turns=[_turn(test=test_id)])

        results, _, reporter = _run([Case(trigger="a", expect=GUARDRAIL)], FakeAgent(script))

        ((_, result),) = results
        assert result.status is Status.ERROR
        assert "delete_account" in result.errors[0]
        reporter.on_warning.assert_called_once()

    def test_unattributed_call_alone_is_not_proof_of_observation(self) -> None:
        """Other tests' records plus one without context: nothing says this test was seen."""

        def script(message: str, test_id: str) -> Window:
            return Window(tools=[_tool("lookup")], turns=[_turn(test="someone-else")])

        results, _, _ = _run([Case(trigger="a", expect=CALLS_LOOKUP)], FakeAgent(script))

        assert results[0][1].status is Status.ERROR

    def test_records_without_context_use_the_whole_window_and_warn_once(self) -> None:
        agent = FakeAgent(lambda message, test_id: Window(tools=[_tool("lookup")]))

        results, _, reporter = _run(
            [Case(trigger="a", expect=CALLS_LOOKUP), Case(trigger="b", expect=CALLS_LOOKUP)],
            agent,
        )

        assert [result.status for _, result in results] == [Status.PASSED, Status.PASSED]
        reporter.on_warning.assert_called_once()
        assert "no test context" in reporter.on_warning.call_args[0][0]

    def test_correlated_window_without_a_record_for_this_test_is_not_observed(self) -> None:
        agent = FakeAgent(lambda message, test_id: Window(turns=[_turn(test="someone-else")]))

        results, _, _ = _run([Case(trigger="a", expect=GUARDRAIL)], agent)

        ((_, result),) = results
        assert result.status is Status.ERROR
        assert "not observed" in result.errors[0]
        assert "/tmp/fake-traces.jsonl" in result.errors[0]

    def test_response_only_test_needs_no_observation(self) -> None:
        agent = FakeAgent(lambda message, test_id: Window(turns=[_turn(test="someone-else")]))

        results, _, _ = _run(
            [Case(trigger="a", expect=Expect(response=ResponseExpect(contains="ok")))], agent
        )

        assert results[0][1].status is Status.PASSED


class TestHello:
    @pytest.fixture(autouse=True)
    def short_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(observation, "HELLO_GRACE_S", 0.2)

    def test_missing_hello_stops_the_run_and_the_agent(self) -> None:
        agent = FakeAgent(hello=False)

        with pytest.raises(AgentNotObservedError, match="nothing was written to /tmp/fake"):
            _run([Case(trigger="a", expect=GUARDRAIL)], agent)

        assert agent.sent == []

    def test_agent_is_stopped_when_the_hello_is_missing(self) -> None:
        agent = FakeAgent(hello=False)
        adapter = MagicMock()
        runner = Runner(
            adapter=adapter,
            client=agent,
            collector=agent,
            assertions=[],
            reporter=MagicMock(),
            settle_time=0,
        )
        spec = Spec(
            agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
            suites=[Suite(name="S", tests=[Case(trigger="a", expect=GUARDRAIL)])],
        )

        with pytest.raises(AgentNotObservedError):
            runner.run(spec)

        adapter.stop.assert_called_once()

    def test_hello_is_not_required_when_no_test_needs_observation(self) -> None:
        agent = FakeAgent(hello=False)

        results, _, _ = _run(
            [Case(trigger="a", expect=Expect(response=ResponseExpect(contains="ok")))], agent
        )

        assert results[0][1].status is Status.PASSED

    def test_hello_is_not_required_in_attach_mode(self) -> None:
        agent = FakeAgent(hello=False)

        results, _, _ = _run([Case(trigger="a", expect=GUARDRAIL)], agent, managed=False)

        assert results[0][1].status is Status.PASSED

    def test_broken_line_before_the_hello_stops_the_run(self) -> None:
        agent = FakeAgent()

        def collect(since: int) -> Window:
            raise TraceFormatError(
                "/tmp/fake-traces.jsonl line 1: not valid JSON (Expecting value)"
            )

        agent.collect = collect  # type: ignore[method-assign]

        with pytest.raises(AgentNotObservedError, match="line 1: not valid JSON"):
            _run([Case(trigger="a", expect=GUARDRAIL)], agent)


def _summary(reporter: MagicMock) -> RunSummary:
    return reporter.on_run_end.call_args[0][0]


def _warnings(reporter: MagicMock) -> list[str]:
    return [c[0][0] for c in reporter.on_warning.call_args_list]


class TestReviewFindings:
    """Paths the PR review found green with the call made, or the call never counted."""

    def test_context_without_the_correlation_keys_counts_by_window(self) -> None:
        """hb.record(..., conversation_id=7) with no middleware: like the bare decorator,
        the window is all there is, and the call counts for the test (C1, second case)."""

        def script(message: str, test_id: str) -> Window:
            return Window(tools=[_tool("lookup", conversation_id="7")])

        results, _, _ = _run([Case(trigger="a", expect=CALLS_LOOKUP)], FakeAgent(script))

        assert results[0][1].status is Status.PASSED

    def test_c1_call_whose_context_lacks_the_test_key_still_fails_a_guardrail(self) -> None:
        """hb.record(..., conversation_id=7) from an executor thread: no 'test' in context."""

        def script(message: str, test_id: str) -> Window:
            return Window(
                tools=[_tool("delete_account", conversation_id=7)],
                turns=[_turn(test=test_id)],
            )

        results, _, _ = _run([Case(trigger="a", expect=GUARDRAIL)], FakeAgent(script))

        ((_, result),) = results
        assert result.status is not Status.PASSED
        assert "delete_account" in result.errors[0]

    def test_c1_v0_line_of_the_reference_emitter_still_fails_a_guardrail(self) -> None:
        def script(message: str, test_id: str) -> Window:
            return Window(
                tools=[_tool("delete_account", conversation_id=None, person_id=None)],
                turns=[_turn(test=test_id)],
            )

        results, _, _ = _run([Case(trigger="a", expect=GUARDRAIL)], FakeAgent(script))

        assert results[0][1].status is not Status.PASSED

    def test_i1_untagged_late_call_of_the_previous_test_does_not_satisfy_the_next(self) -> None:
        def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
            if message == "hi":  # starts cancel_order in a raw thread, after the reply
                return Window(turns=[_turn(test=test_id)]), Window(tools=[_tool("cancel_order")])
            return Window(turns=[_turn(test=test_id)])  # does nothing for this message

        results, _, _ = _run(
            [Case(trigger="hi", expect=SAYS_OK), Case(trigger="cancel", expect=CALLS_CANCEL)],
            FakeAgent(script),
        )

        (_, second) = results[1]
        assert second.status is Status.ERROR
        assert "cancel_order" in second.errors[0]
        assert "cannot tell which test" in second.errors[0]

    def test_c2_late_tagged_call_fails_the_test_that_made_it(self) -> None:
        def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
            if message == "close my account":
                late = Window(tools=[_tool("delete_account", test=test_id)])
                return Window(turns=[_turn(test=test_id)]), late
            return Window(turns=[_turn(test=test_id)])

        results, _, reporter = _run(
            [Case(trigger="close my account", expect=GUARDRAIL), Case(trigger="b", expect=SAYS_OK)],
            FakeAgent(script),
        )

        summary = _summary(reporter)
        assert (summary.passed, summary.failed) == (1, 1)
        (warning,) = _warnings(reporter)
        assert "close my account" in warning and "now fails" in warning
        assert "delete_account" in warning and "settle_time" in warning

    def test_c2_late_call_that_keeps_the_verdict_changes_nothing(self) -> None:
        def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
            now = Window(tools=[_tool("lookup", test=test_id)], turns=[_turn(test=test_id)])
            return now, Window(tools=[_tool("log_event", test=test_id)])

        _, _, reporter = _run(
            [Case(trigger="a", expect=CALLS_LOOKUP), Case(trigger="b", expect=SAYS_OK)],
            FakeAgent(script),
        )

        assert _summary(reporter).passed == 2
        assert _warnings(reporter) == []

    def test_c2_final_read_sees_the_last_tests_late_call(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            return Window(turns=[_turn(test=test_id)]), Window(
                tools=[_tool("delete_account", test=test_id)]
            )

        _, _, reporter = _run([Case(trigger="a", expect=GUARDRAIL)], FakeAgent(script))

        assert _summary(reporter).failed == 1

    def test_c2_untagged_call_after_the_last_test_leaves_it_unverified(self) -> None:
        def script(message: str, test_id: str) -> tuple[Window, Window]:
            return Window(turns=[_turn(test=test_id)]), Window(tools=[_tool("delete_account")])

        _, _, reporter = _run([Case(trigger="a", expect=GUARDRAIL)], FakeAgent(script))

        assert _summary(reporter).errored == 1
        assert "delete_account" in _warnings(reporter)[-1]

    def test_c2_untagged_call_may_be_the_previous_tests(self) -> None:
        """It lands in a response-only test that checks no calls: test 1 is the suspect."""

        def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
            if message == "a":
                return Window(turns=[_turn(test=test_id)]), Window(tools=[_tool("delete_account")])
            return Window(turns=[_turn(test=test_id)])

        _, _, reporter = _run(
            [Case(trigger="a", expect=GUARDRAIL), Case(trigger="b", expect=SAYS_OK)],
            FakeAgent(script),
        )

        summary = _summary(reporter)
        assert (summary.passed, summary.errored) == (1, 1)

    def test_c2_late_call_within_the_same_test_is_in_its_own_result(self) -> None:
        """Phrasing 1's late call lands in phrasing 2's read, before the test is reported."""
        generator = MagicMock()
        generator.generate.return_value = ["b"]

        def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
            if message == "a":
                late = Window(tools=[_tool("delete_account", test=test_id)])
                return Window(turns=[_turn(test=test_id)]), late
            return Window(turns=[_turn(test=test_id)])

        agent, reporter = FakeAgent(script), MagicMock()
        Runner(
            adapter=MagicMock(),
            client=agent,
            collector=agent,
            assertions=[FunctionCallsAssertion()],
            reporter=reporter,
            settle_time=0,
            variations=generator,
        ).run(
            Spec(
                agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
                suites=[Suite(name="S", tests=[Case(trigger="a", expect=GUARDRAIL, variations=1)])],
            )
        )

        (_, result) = reporter.on_test_result.call_args[0]
        assert result.status is Status.FAILED
        assert any("delete_account" in error for error in result.errors)
        assert _warnings(reporter) == []

    def test_not_observed_says_what_was_seen_and_how_to_tag(self) -> None:
        agent = FakeAgent(
            lambda message, test_id: Window(turns=[_turn(test="earlier", conversation_id="9")])
        )

        results, _, _ = _run([Case(trigger="a", expect=GUARDRAIL)], agent)

        message = results[0][1].errors[0]
        assert "context keys: conversation_id, test" in message
        assert "X-Highbar-Test" in message
        assert "--keep-traces" in message
