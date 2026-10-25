"""Test runner — orchestrates the full test execution flow."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from highbar.adapters.file_collector import TraceFormatError
from highbar.adapters.http_client import AgentResponseError, HTTPClient
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
from highbar.engine.observation import (
    AgentNotObservedError,
    Attribution,
    Classified,
    attribute,
    classify,
    describe,
)
from highbar.engine.placeholders import PlaceholderError, Values
from highbar.engine.templates import TemplateError, resolve_template

NO_CONTEXT_WARNING = (
    "some tool calls carry no test context, so each counts for the test whose window it "
    "falls in, and a call that ends after the response can land in the next test. "
    "highbar.asgi.Middleware or hb.turn(test=...) tags them; a thread the agent starts "
    "itself needs contextvars.copy_context() to keep the tag"
)


@dataclass
class _Execution:
    """One execution of a test (one test id), kept for the whole run.

    A call can be recorded after the test's window closed. Keeping what the last turn
    was checked against lets that call still count for the test that made it.
    """

    test_index: int
    label: str  # '[variation: "..."] ' for a phrasing, so its errors say which one
    correlate: dict[str, Any]
    expect: Expect | None = None  # of its last turn
    response: str = ""
    tools: list[Trace] = field(default_factory=list)
    passed: bool = False


@dataclass
class _Finding:
    """An earlier execution judged again, on calls that arrived after its window."""

    execution: _Execution
    status: Status
    error: str


def _names(tools: list[Trace]) -> str:
    return ", ".join(dict.fromkeys(t.fn for t in tools))


def _with_findings(result: TestResult, findings: list[_Finding]) -> TestResult:
    if not findings:
        return result
    return TestResult(
        status=combine([result.status, *(f.status for f in findings)]),
        errors=[*result.errors, *(f.execution.label + f.error for f in findings)],
        duration_ms=result.duration_ms,
        traces=result.traces,
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


def _plain_name(test: Test) -> str:
    """The test's name for {{test.name}} and session hooks: no counts, no decoration."""
    return test.name or test.trigger or f"multi-turn ({len(test.turns or [])} turns)"


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
        self._executions: list[_Execution] = []
        self._findings: list[_Finding] = []
        self._test_index = 0
        # Where the next read starts: each record is read once, and none is missed
        # between two tests (a late call is often written right there).
        self._cursor = 0

    def run(self, spec: Spec) -> RunSummary:
        """Run all suites in the spec."""
        counts = dict.fromkeys(Status, 0)
        reported: dict[int, tuple[str, Status]] = {}
        self._variations_run = 0
        self._warned_no_context = False
        self._executions, self._findings, self._test_index = [], [], 0
        observes = any(test.requires_observation for suite in spec.suites for test in suite.tests)
        run_start = time.time()

        self._adapter.start()
        try:
            if self._managed and observes:
                self._await_hello()
            self._cursor = self._collector.checkpoint()
            for suite in spec.suites:
                self._reporter.on_suite_start(suite)
                for test in suite.tests:
                    self._last_variation_count = None
                    result = self._run_test(test)
                    own = [f for f in self._findings if f.execution.test_index == self._test_index]
                    earlier = [f for f in self._findings if f not in own]
                    # A phrasing's late call lands in the next phrasing's read: it is
                    # part of this test's result, not news about a reported one.
                    result = _with_findings(result, own)
                    name = _test_name(test, self._last_variation_count)
                    counts[result.status] += 1
                    reported[self._test_index] = (name, result.status)
                    self._reporter.on_test_result(name, result)
                    self._report_late(earlier, reported, counts)
                    self._test_index += 1
            if observes and self._executions:
                # The last test has no next one whose read would show its late calls.
                time.sleep(self._settle_time)
                self._read_after_last_test()
                self._report_late(self._findings, reported, counts)
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

    def _report_late(
        self,
        findings: list[_Finding],
        reported: dict[int, tuple[str, Status]],
        counts: dict[Status, int],
    ) -> None:
        """A reported test that passed, and no longer does, is counted again and said so."""
        for finding in findings:
            name, status = reported[finding.execution.test_index]
            if status is not Status.PASSED:
                continue
            counts[Status.PASSED] -= 1
            counts[finding.status] += 1
            reported[finding.execution.test_index] = (name, finding.status)
            verdict = "now fails" if finding.status is Status.FAILED else "is now unverified"
            self._reporter.on_warning(
                f"{name}: {verdict}: {finding.execution.label}{finding.error}"
            )
        self._findings = []

    def _read_after_last_test(self) -> None:
        last = self._executions[-1]
        try:
            window = self._collector.collect(since=self._cursor)
        except TraceFormatError as e:
            if last.passed:
                self._findings.append(_Finding(last, Status.ERROR, str(e)))
            return
        self._cursor = window.end
        untagged = self._untagged(window.tools, last)
        self._judge_late([t for t in window.tools if t not in untagged], current=None)
        self._suspect(last, untagged)

    def _untagged(self, tools: list[Trace], execution: _Execution) -> list[Trace]:
        return [
            t
            for t in tools
            if attribute(t.context, execution.correlate) is Attribution.UNATTRIBUTED
        ]

    def _judge_late(self, tools: list[Trace], current: _Execution | None) -> None:
        """Calls tagged with an earlier execution count for it, though its window closed."""
        late: dict[int, tuple[_Execution, list[Trace]]] = {}
        for tool in tools:
            for execution in reversed(self._executions):
                if execution is current:
                    continue
                if attribute(tool.context, execution.correlate) is Attribution.MINE:
                    late.setdefault(id(execution), (execution, []))[1].append(tool)
                    break
        for execution, calls in late.values():
            was_passing = execution.passed and execution.expect is not None
            execution.tools = [*execution.tools, *calls]
            if not was_passing or execution.expect is None:
                continue
            errors = self._check(execution.tools, execution.response, execution.expect)
            if errors:
                execution.passed = False
                self._findings.append(
                    _Finding(
                        execution,
                        Status.FAILED,
                        f"{_names(calls)} was recorded after the test's window closed "
                        f"(settle_time {self._settle_time * 1000:.0f} ms): "
                        f"{'; '.join(errors)}. Raise agent.settle_time to read it in time",
                    )
                )

    def _suspect(self, execution: _Execution | None, untagged: list[Trace]) -> None:
        """An untagged call may be the late call of ``execution``: if it would fail it,
        that test is unverified."""
        if not untagged or execution is None or not execution.passed or execution.expect is None:
            return
        errors = self._check([*execution.tools, *untagged], execution.response, execution.expect)
        if errors:
            execution.passed = False
            self._findings.append(
                _Finding(
                    execution,
                    Status.ERROR,
                    f"{_names(untagged)} arrived after this test's window with no context "
                    f"saying which test made it, and would fail it ({'; '.join(errors)}). "
                    "A call the agent makes off the request needs "
                    "contextvars.copy_context() to keep the tag",
                )
            )

    def _not_observed(self, classified: Classified, correlate: dict[str, Any]) -> str:
        records = [*classified.other.tools, *classified.other.turns]
        keys = sorted({key for record in records for key in record.context})
        return (
            f"not observed: no trace record in {self._collector.path} matches this test "
            f"({describe(correlate)}); the {len(records)} read while it ran belong to other "
            f"executions (context keys: {', '.join(keys)}). highbar.asgi.Middleware tags "
            "what the agent writes for a message from its X-Highbar-Test header, as "
            "hb.turn(test=<that header>) does. Run with --keep-traces to inspect the file"
        )

    def _check(self, tools: list[Trace], response: str, expect: Expect) -> list[str]:
        return [
            r.message
            for assertion in self._assertions
            for r in assertion.evaluate(tools, response, expect)
            if not r.passed
        ]

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

        name = _plain_name(test)
        if test.turns:
            return self._execute(test.turns, start, labelled=True, name=name)

        if test.trigger is None or test.expect is None:
            raise ValueError("Single-turn test must have both 'trigger' and 'expect'")

        if test.variations:
            return self._run_with_variations(
                test.trigger, test.expect, test.variations, start, name
            )

        return self._execute(
            [Turn(trigger=test.trigger, expect=test.expect)], start, labelled=False, name=name
        )

    def _run_with_variations(
        self, trigger: str, expect: Expect, count: int, start: float, name: str
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
            # The failing phrasing is the whole finding. Without it the report says the
            # intent broke but not which sentence broke it, and it can't be reproduced.
            # Index 0 is the sentence the spec author wrote, not a generated one.
            kind = "trigger" if index == 0 else "variation"
            label = f'[{kind}: "{phrasing}"] '
            # Each phrasing is its own execution, with its own test id.
            result = self._execute(
                [Turn(trigger=phrasing, expect=expect)],
                time.time(),
                labelled=False,
                name=name,
                label=label,
            )
            traces.extend(result.traces)
            statuses.append(result.status)

            if index > 0:
                self._variations_run += 1

            errors.extend(label + error for error in result.errors)

        return TestResult(
            status=combine(statuses),
            errors=errors,
            duration_ms=(time.time() - start) * 1000,
            traces=traces,
        )

    def _execute(
        self, turns: list[Turn], start: float, *, labelled: bool, name: str, label: str = ""
    ) -> TestResult:
        """One execution of a test: one test id, shared by all its turns.

        It stops at the first turn that does not pass: later turns build on that one's
        state, so running them would test a conversation that did not happen.
        ``labelled`` prefixes each error with the turn it came from (multi-turn tests).
        """
        values = Values(message=None, test_id=uuid.uuid4().hex, test_name=name, session={})
        execution = _Execution(self._test_index, label, correlate={"test": values.test_id})
        self._executions.append(execution)
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
            result = self._run_turn(turn.trigger, turn.expect, start, all_traces, values, execution)
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
        values: Values,
        execution: _Execution,
    ) -> TestResult:
        """Send one message, read what the agent recorded for it, and check it."""
        execution.passed = False

        def unverified(error: str) -> TestResult:
            duration = (time.time() - start) * 1000
            return TestResult(status=Status.ERROR, errors=[error], duration_ms=duration)

        try:
            resolved_trigger = resolve_template(trigger, previous_traces)
        except TemplateError as e:
            # Sending the raw "{{...}}" would test a message nobody meant to write; the
            # turn is unverified, and the rest of the run goes on.
            return unverified(str(e))

        try:
            response = self._client.send(
                resolved_trigger, values=replace(values, message=resolved_trigger)
            )
        except AgentResponseError as e:
            return unverified(str(e))
        except PlaceholderError as e:
            return unverified(f"agent.request: {e}")
        except Exception as e:
            # The message never reached the agent, so its behavior was not checked.
            return unverified(f"HTTP error: {e}")

        time.sleep(self._settle_time)
        try:
            window = self._collector.collect(since=self._cursor)
        except TraceFormatError as e:
            # A line that cannot be read may be the call this test is about. The next
            # test starts after it, or every test from here on would stop on it.
            self._cursor = self._collector.checkpoint()
            return unverified(str(e))
        self._cursor = window.end

        classified = classify(window, execution.correlate)
        untagged = classified.unattributed.tools
        if untagged and not self._warned_no_context:
            self._warned_no_context = True
            self._reporter.on_warning(NO_CONTEXT_WARNING)

        if not classified.correlated:
            # Nothing in the window is tagged: it is all there is to go by.
            traces = window.tools
            errors = self._check(traces, response, expect)
        else:
            self._judge_late(classified.other.tools, current=execution)
            if untagged:
                earlier = [e for e in self._executions if e is not execution]
                self._suspect(earlier[-1] if earlier else None, untagged)
            if expect.requires_observation and not (classified.mine.tools or classified.mine.turns):
                return unverified(self._not_observed(classified, execution.correlate))
            traces = classified.mine.tools
            errors = self._check(traces, response, expect)
            if untagged:
                with_them = self._check([*traces, *untagged], response, expect)
                if bool(errors) != bool(with_them):
                    return unverified(
                        f"cannot tell which test made {_names(untagged)}: no "
                        f"{', '.join(execution.correlate)} in its context, and this test "
                        f"{'passes' if errors else 'fails'} with it and "
                        f"{'fails' if errors else 'passes'} without it. A call the agent "
                        "makes off the request needs contextvars.copy_context() to keep the "
                        "tag; hb.record() can also be given it"
                    )

        execution.expect, execution.response = expect, response
        execution.tools, execution.passed = list(traces), not errors
        duration = (time.time() - start) * 1000
        return TestResult(
            status=Status.FAILED if errors else Status.PASSED,
            errors=errors,
            duration_ms=duration,
            traces=traces,
        )
