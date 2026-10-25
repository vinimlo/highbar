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
from highbar.engine.observation import AgentNotObservedError, same_value, select_records
from highbar.engine.runner import TestRunner as Runner
from tests.unit.engine.fakes import FakeAgent

GUARDRAIL = Expect(function_calls=[])
CALLS_LOOKUP = Expect(function_calls=[FunctionCallExpect(name="lookup")])


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


class TestSelectRecords:
    def test_records_are_kept_when_every_correlation_key_matches(self) -> None:
        window = Window(
            tools=[_tool("mine", test="a", conversation_id="7"), _tool("other", test="b")],
            turns=[_turn(test="a"), _turn(test="b")],
        )

        selected, has_context = select_records(window, {"test": "a"})

        assert has_context
        assert [t.fn for t in selected.tools] == ["mine"]
        assert selected.turns == [_turn(test="a")]

    def test_a_window_without_context_is_used_whole(self) -> None:
        window = Window(tools=[_tool("a"), _tool("b")])

        selected, has_context = select_records(window, {"test": "x"})

        assert not has_context
        assert selected == window

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

    def test_records_without_context_use_the_whole_window_and_warn_once(self) -> None:
        agent = FakeAgent(lambda message, test_id: Window(tools=[_tool("lookup")]))

        results, _, reporter = _run(
            [Case(trigger="a", expect=CALLS_LOOKUP), Case(trigger="b", expect=CALLS_LOOKUP)],
            agent,
        )

        assert [result.status for _, result in results] == [Status.PASSED, Status.PASSED]
        reporter.on_warning.assert_called_once()
        assert "no context" in reporter.on_warning.call_args[0][0]

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
