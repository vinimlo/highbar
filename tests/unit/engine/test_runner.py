"""Tests for the test runner orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from highbar.adapters.file_collector import TraceFormatError
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.domain.models import (
    AgentConfig,
    AssertionResult,
    Expect,
    FunctionCallExpect,
    Hello,
    Spec,
    Status,
    Suite,
    Test,
    Trace,
    Turn,
    Window,
)
from highbar.engine.runner import TestRunner


def _make_runner(
    *,
    send_response: str = "ok",
    traces: list[Trace] | None = None,
    assertion_results: list[AssertionResult] | None = None,
) -> tuple[TestRunner, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Create a TestRunner with mocked dependencies."""
    adapter = MagicMock()
    client = MagicMock()
    client.send.return_value = send_response

    collector = MagicMock()
    collector.checkpoint.return_value = 0
    collector.collect.return_value = Window(tools=traces or [], hellos=[Hello(ts=0.0)])

    assertion = MagicMock()
    assertion.evaluate.return_value = assertion_results or [
        AssertionResult(passed=True, message="")
    ]

    reporter = MagicMock()

    runner = TestRunner(
        adapter=adapter,
        client=client,
        collector=collector,
        assertions=[assertion],
        reporter=reporter,
        settle_time=0,
    )
    return runner, adapter, client, collector, reporter


def _make_spec(tests: list[Test], suite_name: str = "Test Suite") -> Spec:
    return Spec(
        agent=AgentConfig(start="python agent.py", endpoint="http://localhost:8000/chat"),
        suites=[Suite(name=suite_name, tests=tests)],
    )


class TestTestRunner:
    def test_single_test_passing(self) -> None:
        runner, adapter, _, _, reporter = _make_runner()
        spec = _make_spec([Test(trigger="hello", expect=Expect())])

        summary = runner.run(spec)

        assert summary.total == 1
        assert summary.passed == 1
        assert summary.failed == 0
        adapter.start.assert_called_once()
        adapter.stop.assert_called_once()

    def test_single_test_failing(self) -> None:
        runner, _, _, _, reporter = _make_runner(
            assertion_results=[AssertionResult(passed=False, message="Expected X")]
        )
        spec = _make_spec([Test(trigger="hello", expect=Expect())])

        summary = runner.run(spec)

        assert summary.total == 1
        assert summary.passed == 0
        assert summary.failed == 1

    def test_multi_turn_passes(self) -> None:
        runner, _, _, _, _ = _make_runner()
        test = Test(
            turns=[
                Turn(trigger="turn 1", expect=Expect()),
                Turn(trigger="turn 2", expect=Expect()),
            ]
        )
        spec = _make_spec([test])

        summary = runner.run(spec)

        assert summary.total == 1
        assert summary.passed == 1

    def test_multi_turn_fails_early_exit(self) -> None:
        """When a turn fails, subsequent turns should not execute."""
        runner, _, client, _, _ = _make_runner(
            assertion_results=[AssertionResult(passed=False, message="failed")]
        )
        test = Test(
            turns=[
                Turn(trigger="turn 1", expect=Expect()),
                Turn(trigger="turn 2", expect=Expect()),
            ]
        )
        spec = _make_spec([test])

        summary = runner.run(spec)

        assert summary.failed == 1
        # Only first turn should have been executed
        assert client.send.call_count == 1

    def test_http_error_counts_as_errored(self) -> None:
        runner, _, client, _, _ = _make_runner()
        client.send.side_effect = ConnectionError("refused")
        spec = _make_spec([Test(trigger="hello", expect=Expect())])

        summary = runner.run(spec)

        assert (summary.failed, summary.errored) == (0, 1)

    def test_adapter_stop_always_called(self) -> None:
        """Adapter.stop() must be called even if tests fail."""
        runner, adapter, client, _, _ = _make_runner()
        client.send.side_effect = Exception("boom")
        spec = _make_spec([Test(trigger="hello", expect=Expect())])

        runner.run(spec)

        adapter.stop.assert_called_once()

    def test_summary_counts(self) -> None:
        """Multiple tests should be counted correctly."""
        call_count = 0
        results_cycle = [
            [AssertionResult(passed=True, message="")],
            [AssertionResult(passed=False, message="fail")],
            [AssertionResult(passed=True, message="")],
        ]

        assertion = MagicMock()

        def side_effect(*args, **kwargs):
            nonlocal call_count
            result = results_cycle[call_count % len(results_cycle)]
            call_count += 1
            return result

        assertion.evaluate.side_effect = side_effect

        adapter = MagicMock()
        client = MagicMock()
        client.send.return_value = "ok"
        collector = MagicMock()
        collector.checkpoint.return_value = 0
        collector.collect.return_value = Window()
        reporter = MagicMock()

        runner = TestRunner(
            adapter=adapter,
            client=client,
            collector=collector,
            assertions=[assertion],
            reporter=reporter,
            settle_time=0,
        )

        spec = _make_spec(
            [
                Test(trigger="t1", expect=Expect()),
                Test(trigger="t2", expect=Expect()),
                Test(trigger="t3", expect=Expect()),
            ]
        )

        summary = runner.run(spec)

        assert summary.total == 3
        assert summary.passed == 2
        assert summary.failed == 1

    def test_http_error_is_error_not_failed(self) -> None:
        """The agent was never asked, so nothing was verified: that is not a failure."""
        runner, _, client, _, reporter = _make_runner()
        client.send.side_effect = httpx.ConnectError("connection refused")
        spec = _make_spec([Test(trigger="hello", expect=Expect())])

        runner.run(spec)

        _, result = reporter.on_test_result.call_args[0]
        assert result.status is Status.ERROR
        assert "connection refused" in result.errors[0]

    def test_assertion_failure_is_failed(self) -> None:
        called_b = Trace(fn="b", args={}, result={}, started_at=1.0, ended_at=1.1)
        runner, _, _, _, reporter = _make_runner(traces=[called_b])
        runner._assertions = [FunctionCallsAssertion()]
        expect = Expect(function_calls=[FunctionCallExpect(name="a")])
        spec = _make_spec([Test(trigger="hello", expect=expect)])

        runner.run(spec)

        _, result = reporter.on_test_result.call_args[0]
        assert result.status is Status.FAILED

    def test_summary_counts_errors_separately(self) -> None:
        runner, _, client, _, _ = _make_runner()

        def send(trigger: str, **_: object) -> str:
            if trigger == "unreachable":
                raise httpx.ConnectError("connection refused")
            return trigger

        client.send.side_effect = send
        assertion = MagicMock()
        assertion.evaluate.side_effect = lambda traces, response, expect: [
            AssertionResult(passed=response != "wrong", message="Expected X")
        ]
        runner._assertions = [assertion]
        spec = _make_spec(
            [
                Test(trigger="right", expect=Expect()),
                Test(trigger="wrong", expect=Expect()),
                Test(trigger="unreachable", expect=Expect()),
            ]
        )

        summary = runner.run(spec)

        assert (summary.total, summary.passed, summary.failed, summary.errored) == (3, 1, 1, 1)

    def test_a5_template_error_is_error_and_run_continues(self) -> None:
        """A turn that refers to a call that never happened was not tested: error, not crash."""
        runner, _, client, _, reporter = _make_runner()
        broken = Test(
            turns=[
                Turn(trigger="find my order", expect=Expect()),
                Turn(trigger="cancel {{previous.function_calls[0].result.id}}", expect=Expect()),
            ]
        )
        spec = _make_spec([broken, Test(trigger="hello", expect=Expect())])

        summary = runner.run(spec)

        (_, first), (_, second) = [c[0] for c in reporter.on_test_result.call_args_list]
        assert first.status is Status.ERROR
        assert "function_calls[0]" in first.errors[0]
        assert "only 0 calls" in first.errors[0]
        assert second.status is Status.PASSED
        assert summary.errored == 1
        # The unresolvable message is never sent: turn 1 and the second test only.
        assert client.send.call_count == 2

    def test_r2_multi_turn_failure_names_the_turn(self) -> None:
        found = Trace(fn="find_order", args={}, result={"id": 7}, started_at=1.0, ended_at=1.1)
        runner, _, client, _, reporter = _make_runner(traces=[found])
        client.send.side_effect = lambda trigger, **_: trigger
        assertion = MagicMock()
        assertion.evaluate.side_effect = lambda traces, response, expect: [
            AssertionResult(passed=response != "refund order 7", message="Expected X")
        ]
        runner._assertions = [assertion]
        test = Test(
            turns=[
                Turn(trigger="find my order", expect=Expect()),
                Turn(
                    trigger="refund order {{previous.function_calls[0].result.id}}", expect=Expect()
                ),
                Turn(trigger="thanks", expect=Expect()),
            ]
        )

        runner.run(_make_spec([test]))

        _, result = reporter.on_test_result.call_args[0]
        assert result.errors[0].startswith("turn 2/3 (refund order 7): ")

    def test_r2_turn_name_labels_the_failure(self) -> None:
        runner, _, _, _, reporter = _make_runner(
            assertion_results=[AssertionResult(passed=False, message="Expected X")]
        )
        test = Test(turns=[Turn(trigger="x" * 200, expect=Expect(), name="greeting")])

        runner.run(_make_spec([test]))

        _, result = reporter.on_test_result.call_args[0]
        assert result.errors[0] == "turn 1/1 (greeting): Expected X"

    def test_r2_long_trigger_label_is_cut_at_80(self) -> None:
        runner, _, _, _, reporter = _make_runner(
            assertion_results=[AssertionResult(passed=False, message="Expected X")]
        )
        test = Test(turns=[Turn(trigger="y" * 200, expect=Expect())])

        runner.run(_make_spec([test]))

        _, result = reporter.on_test_result.call_args[0]
        assert result.errors[0] == f"turn 1/1 ({'y' * 80}\u2026): Expected X"

    def test_r2_test_name_is_used_in_report(self) -> None:
        runner, _, _, _, reporter = _make_runner()
        spec = _make_spec([Test(name="cancel order", trigger="cancel it", expect=Expect())])

        runner.run(spec)

        name, _ = reporter.on_test_result.call_args[0]
        assert name == "cancel order"

    def test_broken_trace_line_errors_that_test_and_the_run_goes_on(self) -> None:
        """A trace line that cannot be read leaves that test unverified, not the run aborted."""
        runner, _, _, collector, reporter = _make_runner()
        collector.collect.side_effect = [
            TraceFormatError("/tmp/t.jsonl line 2: not valid JSON (Expecting value)"),
            Window(),
        ]
        spec = _make_spec(
            [Test(trigger="one", expect=Expect()), Test(trigger="two", expect=Expect())]
        )

        summary = runner.run(spec)

        (_, first), (_, second) = [c[0] for c in reporter.on_test_result.call_args_list]
        assert first.status is Status.ERROR
        assert "line 2" in first.errors[0]
        assert second.status is Status.PASSED
        assert summary.errored == 1

    def test_reporter_methods_called(self) -> None:
        runner, _, _, _, reporter = _make_runner()
        spec = _make_spec([Test(trigger="hello", expect=Expect())])

        runner.run(spec)

        reporter.on_suite_start.assert_called_once()
        reporter.on_test_result.assert_called_once()
        reporter.on_run_end.assert_called_once()
