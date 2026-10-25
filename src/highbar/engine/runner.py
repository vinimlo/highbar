"""Test runner — orchestrates the full test execution flow."""

from __future__ import annotations

import time

from highbar.adapters.http_client import HTTPClient
from highbar.adapters.subprocess_adapter import SubprocessAdapter
from highbar.domain.models import (
    Expect,
    RunSummary,
    Spec,
    Status,
    Test,
    TestResult,
    Trace,
    Turn,
    combine,
)
from highbar.domain.protocols import Assertion, Reporter, TraceCollector, VariationGenerator
from highbar.engine.templates import TemplateError, resolve_template


def _test_name(test: Test, variations_run: int | None = None) -> str:
    """Display name. ``variations_run`` is how many paraphrases were actually executed.

    The title reports that number, not the one the spec asked for: when the generator
    produced 3 of the 5 requested, saying "5 variations" overstates what was tested.
    """
    if test.turns:
        return f"multi-turn ({len(test.turns)} turns)"
    if test.variations:
        if variations_run is None or variations_run == test.variations:
            return f"{test.trigger} ({test.variations} variations)"
        return f"{test.trigger} ({variations_run} of {test.variations} variations)"
    return test.trigger or ""


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
        variations: VariationGenerator | None = None,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._collector = collector
        self._assertions = assertions
        self._reporter = reporter
        self._settle_time = settle_time
        self._variations = variations
        self._variations_run = 0
        self._last_variation_count: int | None = None

    def run(self, spec: Spec) -> RunSummary:
        """Run all suites in the spec."""
        counts = dict.fromkeys(Status, 0)
        self._variations_run = 0
        run_start = time.time()

        self._adapter.start()
        try:
            for suite in spec.suites:
                self._reporter.on_suite_start(suite)
                for test in suite.tests:
                    self._last_variation_count = None
                    result = self._run_test(test)
                    counts[result.status] += 1
                    self._reporter.on_test_result(
                        _test_name(test, self._last_variation_count), result
                    )
        finally:
            self._adapter.stop()

        run_duration = (time.time() - run_start) * 1000
        summary = RunSummary(
            total=sum(counts.values()),
            passed=counts[Status.PASSED],
            failed=counts[Status.FAILED],
            errored=counts[Status.ERROR],
            duration_ms=run_duration,
            variations=self._variations_run,
        )
        self._reporter.on_run_end(summary)
        return summary

    def _run_test(self, test: Test) -> TestResult:
        """Run a single test (single-turn, multi-turn, or a trigger plus its variations)."""
        start = time.time()

        if test.turns:
            return self._run_multi_turn(test.turns, start)

        if test.trigger is None or test.expect is None:
            raise ValueError("Single-turn test must have both 'trigger' and 'expect'")

        if test.variations:
            return self._run_with_variations(test.trigger, test.expect, test.variations, start)

        return self._run_single_turn(test.trigger, test.expect, start, [])

    def _run_with_variations(
        self, trigger: str, expect: Expect, count: int, start: float
    ) -> TestResult:
        """Run the trigger and N paraphrases of it through the same assertions.

        The test fails if ANY phrasing fails: an agent that handles nine wordings of a
        request and drops the tenth is broken for whoever typed the tenth.

        Every phrasing is run even after one fails — knowing that three of ten broke is
        a different bug from one of ten, and stopping early hides which.

        All phrasings go to the same running agent, back to back, with no reset in
        between. That is sound for intents that only read; for one that changes state,
        a later phrasing can fail because of what an earlier one did.
        """
        if self._variations is None:
            msg = (
                f"Test asks for {count} variations but no variation generator is "
                "configured. See `highbar variations <spec>`."
            )
            raise ValueError(msg)

        # A missing cache, a missing key or a failed LLM call leaves this test unverified,
        # not a reason to abort the run and lose the results of every other test.
        try:
            paraphrases = self._variations.generate(trigger, count)
        except Exception as e:
            self._last_variation_count = 0
            return TestResult(
                status=Status.ERROR,
                errors=[f"Could not get variations: {e}"],
                duration_ms=(time.time() - start) * 1000,
            )

        self._last_variation_count = len(paraphrases)
        triggers = [trigger, *paraphrases]

        errors: list[str] = []
        traces: list[Trace] = []
        statuses: list[Status] = []

        for index, phrasing in enumerate(triggers):
            result = self._run_single_turn(phrasing, expect, time.time(), [])
            traces.extend(result.traces)
            statuses.append(result.status)

            if index > 0:
                self._variations_run += 1

            # The failing phrasing is the whole finding. Without it the report says the
            # intent broke but not which sentence broke it, and it can't be reproduced.
            # Index 0 is the sentence the spec author wrote, not a generated one.
            label = "trigger" if index == 0 else "variation"
            errors.extend(f'[{label}: "{phrasing}"] {error}' for error in result.errors)

        return TestResult(
            status=combine(statuses),
            errors=errors,
            duration_ms=(time.time() - start) * 1000,
            traces=traces,
        )

    def _run_single_turn(
        self, trigger: str, expect: Expect, start: float, previous_traces: list[Trace]
    ) -> TestResult:
        """Run a single trigger-expect pair."""
        try:
            resolved_trigger = resolve_template(trigger, previous_traces)
        except TemplateError as e:
            # Sending the raw "{{...}}" would test a message nobody meant to write; the
            # turn is unverified, and the rest of the run goes on.
            duration = (time.time() - start) * 1000
            return TestResult(status=Status.ERROR, errors=[str(e)], duration_ms=duration)

        checkpoint = self._collector.checkpoint()

        try:
            response = self._client.send(resolved_trigger)
        except Exception as e:
            # The message never reached the agent, so its behavior was not checked.
            duration = (time.time() - start) * 1000
            return TestResult(
                status=Status.ERROR, errors=[f"HTTP error: {e}"], duration_ms=duration
            )

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
            status=Status.FAILED if errors else Status.PASSED,
            errors=errors,
            duration_ms=duration,
            traces=traces,
        )

    def _run_multi_turn(self, turns: list[Turn], start: float) -> TestResult:
        """Run a multi-turn test, threading traces between turns.

        It stops at the first turn that does not pass: later turns build on that one's
        state, so running them would test a conversation that did not happen.
        """
        all_traces: list[Trace] = []
        all_errors: list[str] = []
        statuses: list[Status] = []

        for turn in turns:
            result = self._run_single_turn(turn.trigger, turn.expect, start, all_traces)
            all_traces.extend(result.traces)
            all_errors.extend(result.errors)
            statuses.append(result.status)

            if not result.passed:
                break

        duration = (time.time() - start) * 1000
        return TestResult(
            status=combine(statuses),
            errors=all_errors,
            duration_ms=duration,
            traces=all_traces,
        )
