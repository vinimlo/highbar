"""Test runner — orchestrates the full test execution flow."""

from __future__ import annotations

import time
import uuid

from highbar.adapters.file_collector import TraceFormatError
from highbar.adapters.http_client import HTTPClient
from highbar.adapters.subprocess_adapter import SubprocessAdapter
from highbar.assertions.response import truncate
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
from highbar.engine import observation
from highbar.engine.observation import AgentNotObservedError, describe, select_records
from highbar.engine.templates import TemplateError, resolve_template

NO_CONTEXT_WARNING = (
    "some tool calls carry no test context, so each counts for the test whose window it "
    "falls in, and a call that ends after the response can land in the next test. "
    "highbar.asgi.Middleware or hb.turn(test=...) tags them; a thread the agent starts "
    "itself needs contextvars.copy_context() to keep the tag"
)


def _test_name(test: Test, variations_run: int | None = None) -> str:
    """Display name. ``variations_run`` is how many paraphrases were actually executed.

    The title reports that number, not the one the spec asked for: when the generator
    produced 3 of the 5 requested, saying "5 variations" overstates what was tested.
    """
    if test.turns:
        return test.name or f"multi-turn ({len(test.turns)} turns)"
    label = test.name or test.trigger or ""
    if test.variations:
        if variations_run is None or variations_run == test.variations:
            return f"{label} ({test.variations} variations)"
        return f"{label} ({variations_run} of {test.variations} variations)"
    return label


def _turn_label(turn: Turn, previous_traces: list[Trace]) -> str:
    """The turn's ``name``, or the message it sends, cut at 80 characters."""
    if turn.name:
        return turn.name
    try:
        trigger = resolve_template(turn.trigger, previous_traces)
    except TemplateError:
        trigger = turn.trigger
    return truncate(trigger, 80)


class TestRunner:
    """Orchestrates test execution: start agent, send triggers, collect traces, assert.

    ``managed`` says highbar started the agent itself. It then knows the trace file is
    new, and requires the SDK's `hello` in it before any test that checks tool calls.
    """

    def __init__(
        self,
        adapter: SubprocessAdapter,
        client: HTTPClient,
        collector: TraceCollector,
        assertions: list[Assertion],
        reporter: Reporter,
        settle_time: float = 0.5,
        variations: VariationGenerator | None = None,
        *,
        managed: bool = True,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._collector = collector
        self._assertions = assertions
        self._reporter = reporter
        self._settle_time = settle_time
        self._variations = variations
        self._managed = managed
        self._variations_run = 0
        self._last_variation_count: int | None = None
        self._warned_no_context = False

    def run(self, spec: Spec) -> RunSummary:
        """Run all suites in the spec."""
        counts = dict.fromkeys(Status, 0)
        self._variations_run = 0
        self._warned_no_context = False
        run_start = time.time()

        self._adapter.start()
        try:
            if self._managed and any(
                test.requires_observation for suite in spec.suites for test in suite.tests
            ):
                self._await_hello()
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

    def _await_hello(self) -> None:
        """The agent highbar started must show its SDK's `hello` in the new trace file.

        Otherwise a guardrail (``function_calls: []``) would pass against an agent that
        records nothing: an old server on the port, or an SDK that never started.
        """
        deadline = time.monotonic() + observation.HELLO_GRACE_S
        while True:
            try:
                if self._collector.collect(since=0).hellos:
                    return
            except TraceFormatError as e:
                raise AgentNotObservedError(str(e)) from None
            if time.monotonic() >= deadline:
                raise AgentNotObservedError(
                    f"the agent answered, but nothing was written to {self._collector.path}: "
                    "another process on the port, or Highbar() was never created "
                    "(it reads HIGHBAR_TRACES_URL when constructed)"
                )
            time.sleep(0.1)

    def _run_test(self, test: Test) -> TestResult:
        """Run a single test (single-turn, multi-turn, or a trigger plus its variations)."""
        start = time.time()

        if test.turns:
            return self._execute(test.turns, start, labelled=True)

        if test.trigger is None or test.expect is None:
            raise ValueError("Single-turn test must have both 'trigger' and 'expect'")

        if test.variations:
            return self._run_with_variations(test.trigger, test.expect, test.variations, start)

        return self._execute(
            [Turn(trigger=test.trigger, expect=test.expect)], start, labelled=False
        )

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
            # Each phrasing is its own execution, with its own test id.
            result = self._execute(
                [Turn(trigger=phrasing, expect=expect)], time.time(), labelled=False
            )
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

    def _execute(self, turns: list[Turn], start: float, *, labelled: bool) -> TestResult:
        """One execution of a test: one test id, shared by all its turns.

        It stops at the first turn that does not pass: later turns build on that one's
        state, so running them would test a conversation that did not happen.
        ``labelled`` prefixes each error with the turn it came from (multi-turn tests).
        """
        test_id = uuid.uuid4().hex
        all_traces: list[Trace] = []
        all_errors: list[str] = []
        statuses: list[Status] = []

        for number, turn in enumerate(turns, start=1):
            # Which turn broke is the finding: "Expected X" alone does not say where.
            prefix = (
                f"turn {number}/{len(turns)} ({_turn_label(turn, all_traces)}): "
                if labelled
                else ""
            )
            result = self._run_turn(turn.trigger, turn.expect, start, all_traces, test_id)
            all_traces.extend(result.traces)
            all_errors.extend(prefix + error for error in result.errors)
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

    def _run_turn(
        self,
        trigger: str,
        expect: Expect,
        start: float,
        previous_traces: list[Trace],
        test_id: str,
    ) -> TestResult:
        """Send one message, read what the agent recorded for it, and check it."""

        def unverified(error: str) -> TestResult:
            duration = (time.time() - start) * 1000
            return TestResult(status=Status.ERROR, errors=[error], duration_ms=duration)

        try:
            resolved_trigger = resolve_template(trigger, previous_traces)
        except TemplateError as e:
            # Sending the raw "{{...}}" would test a message nobody meant to write; the
            # turn is unverified, and the rest of the run goes on.
            return unverified(str(e))

        checkpoint = self._collector.checkpoint()

        try:
            response = self._client.send(resolved_trigger, test_id=test_id)
        except Exception as e:
            # The message never reached the agent, so its behavior was not checked.
            return unverified(f"HTTP error: {e}")

        time.sleep(self._settle_time)
        try:
            window = self._collector.collect(since=checkpoint)
        except TraceFormatError as e:
            # A line that cannot be read may be the call this test is about.
            return unverified(str(e))

        correlate = {"test": test_id}
        selection = select_records(window, correlate)
        if any(not t.context for t in window.tools) and not self._warned_no_context:
            self._warned_no_context = True
            self._reporter.on_warning(NO_CONTEXT_WARNING)

        if expect.requires_observation and selection.has_context and not selection.matched:
            return unverified(
                f"not observed: no trace record for this test ({describe(correlate)}) in "
                f"{self._collector.path}; the records there belong to other tests"
            )

        traces = selection.window.tools
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
