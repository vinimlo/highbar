"""Tests for the test runner orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

from highbar.domain.models import (
    AgentConfig,
    AssertionResult,
    Expect,
    Spec,
    Suite,
    Test,
    Trace,
    Turn,
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
    collector.collect.return_value = traces or []

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

    def test_http_error_returns_failure(self) -> None:
        runner, _, client, _, _ = _make_runner()
        client.send.side_effect = ConnectionError("refused")
        spec = _make_spec([Test(trigger="hello", expect=Expect())])

        summary = runner.run(spec)

        assert summary.failed == 1

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
        collector.collect.return_value = []
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

    def test_reporter_methods_called(self) -> None:
        runner, _, _, _, reporter = _make_runner()
        spec = _make_spec([Test(trigger="hello", expect=Expect())])

        runner.run(spec)

        reporter.on_suite_start.assert_called_once()
        reporter.on_test_result.assert_called_once()
        reporter.on_run_end.assert_called_once()
