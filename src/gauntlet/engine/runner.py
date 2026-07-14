"""Test runner — orchestrates the full test execution flow."""

from __future__ import annotations

import time

from gauntlet.adapters.http_client import HTTPClient
from gauntlet.adapters.subprocess_adapter import SubprocessAdapter
from gauntlet.domain.models import (
    Expect,
    RunSummary,
    Spec,
    Test,
    TestResult,
    Trace,
    Turn,
)
from gauntlet.domain.protocols import Assertion, Reporter, TraceCollector
from gauntlet.engine.templates import resolve_template


class TestRunner:
    """Orchestrates test execution: start agent, send triggers, collect traces, assert."""

    def __init__(
        self,
        adapter: SubprocessAdapter,
        client: HTTPClient,
        collector: TraceCollector,
        assertions: list[Assertion],
        reporter: Reporter,
        settle_time: float = 0.5,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._collector = collector
        self._assertions = assertions
        self._reporter = reporter
        self._settle_time = settle_time

    def run(self, spec: Spec) -> RunSummary:
        """Run all suites in the spec."""
        total = 0
        passed = 0
        failed = 0
        run_start = time.time()

        self._adapter.start()
        try:
            for suite in spec.suites:
                self._reporter.on_suite_start(suite)
                for test in suite.tests:
                    total += 1
                    test_name = test.trigger or f"multi-turn ({len(test.turns or [])} turns)"
                    result = self._run_test(test)
                    if result.passed:
                        passed += 1
                    else:
                        failed += 1
                    self._reporter.on_test_result(test_name, result)
        finally:
            self._adapter.stop()

        run_duration = (time.time() - run_start) * 1000
        summary = RunSummary(
            total=total,
            passed=passed,
            failed=failed,
            duration_ms=run_duration,
        )
        self._reporter.on_run_end(summary)
        return summary

    def _run_test(self, test: Test) -> TestResult:
        """Run a single test (single-turn or multi-turn)."""
        start = time.time()

        if test.turns:
            return self._run_multi_turn(test.turns, start)

        if test.trigger is None or test.expect is None:
            raise ValueError("Single-turn test must have both 'trigger' and 'expect'")
        return self._run_single_turn(test.trigger, test.expect, start, [])

    def _run_single_turn(
        self, trigger: str, expect: Expect, start: float, previous_traces: list[Trace]
    ) -> TestResult:
        """Run a single trigger-expect pair."""
        resolved_trigger = resolve_template(trigger, previous_traces)

        checkpoint = self._collector.checkpoint()

        try:
            response = self._client.send(resolved_trigger)
        except Exception as e:
            duration = (time.time() - start) * 1000
            return TestResult(passed=False, errors=[f"HTTP error: {e}"], duration_ms=duration)

        time.sleep(self._settle_time)
        traces = self._collector.collect(since=checkpoint)

        errors: list[str] = []
        for assertion in self._assertions:
            results = assertion.evaluate(traces, response, expect)
            for r in results:
                if not r.passed:
                    errors.append(r.message)

        duration = (time.time() - start) * 1000
        return TestResult(
            passed=len(errors) == 0,
            errors=errors,
            duration_ms=duration,
            traces=traces,
        )

    def _run_multi_turn(self, turns: list[Turn], start: float) -> TestResult:
        """Run a multi-turn test, threading traces between turns."""
        all_traces: list[Trace] = []
        all_errors: list[str] = []

        for turn in turns:
            result = self._run_single_turn(turn.trigger, turn.expect, start, all_traces)
            all_traces.extend(result.traces)
            all_errors.extend(result.errors)

            if not result.passed:
                break

        duration = (time.time() - start) * 1000
        failed = len(all_errors) > 0
        return TestResult(
            passed=not failed,
            errors=all_errors,
            duration_ms=duration,
            traces=all_traces,
        )
