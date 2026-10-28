"""Test runner — orchestrates the full test execution flow."""

from __future__ import annotations

import difflib
import time
import uuid
from collections.abc import Mapping
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
    Suite,
    Test,
    TestResult,
    Trace,
    Turn,
    Window,
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
    same_value,
)
from highbar.engine.placeholders import PlaceholderError, Values, render
from highbar.engine.runs import phrasing_status, resolve_runs
from highbar.engine.secrets import redact
from highbar.engine.session import SessionContext, SessionError, SessionHooks
from highbar.engine.templates import TemplateError, resolve_template

# A window with no tagged record: its untagged calls are all there is, and count.
NO_CONTEXT_WARNING = (
    "some tool calls carry {untagged}, so each counts for the test whose window it falls "
    "in, and a call that ends after the response can land in the next test; {hint}; a "
    "thread the agent starts itself needs contextvars.copy_context() to keep the tag"
)

# Next to tagged records, an untagged call counts for no test (PR 14 review I1).
UNATTRIBUTED_WARNING = (
    "some tool calls carry {untagged}, so they count for no test, and a test whose result "
    "they would change is an error; {hint}; a thread the agent starts itself needs "
    "contextvars.copy_context() to keep the tag"
)

UNDECLARED_WARNING = (
    "the agent has not declared the tools it records, so these guardrails cannot tell a "
    "tool it never records from one it did not call: {names}. highbar's Python SDK "
    "declares them after each hello since 0.1.0rc2; another writer adds a `tools` record "
    "(see the trace contract)"
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
    phrasing: int = 0  # which phrasing of the test, and which run of it
    run: int = 0
    expect: Expect | None = None  # of its last turn
    response: str = ""
    tools: list[Trace] = field(default_factory=list[Trace])
    passed: bool = False


@dataclass
class _Finding:
    """An earlier execution judged again, on calls that arrived after its window."""

    execution: _Execution
    status: Status
    error: str


class _Masking:
    """A reporter's stand-in that masks each value from ${VAR} in what it is told."""

    def __init__(self, reporter: Reporter, secrets: Mapping[str, str]) -> None:
        self._reporter = reporter
        self._secrets = secrets

    def on_suite_start(self, suite: Suite) -> None:
        self._reporter.on_suite_start(suite)

    def on_test_result(self, test_name: str, result: TestResult) -> None:
        errors = [redact(error, self._secrets) for error in result.errors]
        self._reporter.on_test_result(test_name, replace(result, errors=errors))

    def on_warning(self, message: str) -> None:
        self._reporter.on_warning(redact(message, self._secrets))

    def on_run_end(self, summary: RunSummary) -> None:
        self._reporter.on_run_end(summary)


def _same_correlation(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return bool(a) and a.keys() == b.keys() and all(same_value(a[k], b[k]) for k in a)


def _names(tools: list[Trace]) -> str:
    return ", ".join(dict.fromkeys(t.fn for t in tools))


def _is_default(correlate: Mapping[Any, Any]) -> bool:
    return [str(key) for key in correlate] == ["test"]


def _untagged(correlate: Mapping[Any, Any]) -> str:
    """What untagged records lack, in the words of the spec's agent.correlate."""
    if _is_default(correlate):
        return "no test context"
    return f"none of agent.correlate's keys ({', '.join(str(key) for key in correlate)})"


def _tag_hint(correlate: Mapping[Any, Any]) -> str:
    """How records get the context that agent.correlate matches on (FR2)."""
    if _is_default(correlate):
        return (
            "tag them with the test id: highbar.asgi.Middleware, or "
            "hb.turn(test=<the X-Highbar-Test header>)"
        )
    keys = [str(key) for key in correlate]
    fields = ", ".join(f"{key}=..." for key in keys)
    return (
        f"give their context {', '.join(keys)} (agent.correlate): hb.turn({fields}) around "
        "the handling of each message, or a `context` field from another writer"
    )


def _merged_errors(results: list[TestResult]) -> list[str]:
    """The errors of a phrasing's runs; one that repeats says in how many runs."""
    if len(results) == 1:
        return list(results[0].errors)
    seen: dict[str, int] = {}
    for result in results:
        for error in dict.fromkeys(result.errors):
            seen[error] = seen.get(error, 0) + 1
    return [f"{error} ({n} of {len(results)} runs)" for error, n in seen.items()]


@dataclass
class _Gate:
    """The status of each run of a test, per phrasing, kept for the whole run.

    A late call fails its own run, and the test is judged again against min_pass:
    a run that fails late counts like a run that fails in time, no more.
    """

    min_pass: int
    counted: bool  # the title shows passed/runs: no variations, more than one run
    runs: list[list[Status]] = field(default_factory=list[list[Status]])

    @property
    def status(self) -> Status:
        return combine(phrasing_status(statuses, self.min_pass) for statuses in self.runs)

    @property
    def counts(self) -> tuple[int, int, int] | None:
        if not self.counted:
            return None
        (statuses,) = self.runs
        return sum(s is Status.PASSED for s in statuses), len(statuses), self.min_pass

    def demote(self, finding: _Finding) -> None:
        execution = finding.execution
        run = self.runs[execution.phrasing]
        run[execution.run] = combine([run[execution.run], finding.status])

    def describe(self, finding: _Finding) -> str:
        execution = finding.execution
        total = len(self.runs[execution.phrasing])
        which = f" (run {execution.run + 1} of {total})" if total > 1 else ""
        return f"{execution.label}{finding.error}{which}"


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
        session: SessionHooks | None = None,
        spec_path: str = "",
        correlate: Mapping[str, Any] | None = None,
        secrets: Mapping[str, str] | None = None,
        default_runs: int = 1,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._collector = collector
        self._assertions = assertions
        # Every message passes through it: values from ${VAR} never reach the output.
        self._reporter = _Masking(reporter, secrets) if secrets else reporter
        self._settle_time = settle_time
        self._variations = variations
        self._managed = managed
        self._session = session
        self._spec_path = spec_path
        # Which context keys identify a test's records (agent.correlate in the spec).
        self._correlate = dict(correlate) if correlate is not None else {"test": "{{test.id}}"}
        self._default_runs = default_runs
        # The spec's defaults.min_pass, set by run(): the value check_runs checked.
        self._default_min_pass: int | float | None = None
        self._variations_run = 0
        self._last_variation_count: int | None = None
        self._warned: set[str] = set()
        # The tools the agent declared it records (None until it declares any), and the
        # ones seen in its records: what a name in the spec is checked against (D4).
        self._inventory: set[str] | None = None
        self._seen: set[str] = set()
        # Forbidden names checked while the agent had declared nothing: warned at the end.
        self._undeclared: dict[str, None] = {}
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
        self._default_min_pass = spec.default_min_pass
        self._warned = set()
        self._inventory, self._seen, self._undeclared = None, set(), {}
        self._executions, self._findings, self._test_index = [], [], 0
        self._gates: dict[int, _Gate] = {}
        observes = any(test.requires_observation for suite in spec.suites for test in suite.tests)
        run_start = time.time()

        self._adapter.start()
        try:
            if self._managed and observes:
                self._await_hello()
            elif observes and self._collector.missing():
                raise AgentNotObservedError(self._missing_file())
            self._cursor = self._collector.checkpoint()
            if observes:
                self._learn_inventory()
            for suite in spec.suites:
                self._reporter.on_suite_start(suite)
                for test in suite.tests:
                    self._last_variation_count = None
                    result = self._run_test(test)
                    own = [f for f in self._findings if f.execution.test_index == self._test_index]
                    earlier = [f for f in self._findings if f not in own]
                    self._findings = []
                    # A run's late call lands in the next run's read: it is part of this
                    # test's result, not news about a reported one.
                    if own:
                        gate = self._gates[self._test_index]
                        for finding in own:
                            gate.demote(finding)
                        result = replace(
                            result,
                            status=gate.status,
                            counts=gate.counts,
                            errors=[*result.errors, *(gate.describe(f) for f in own)],
                        )
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
            self._warn_undeclared()
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
        """Judge a reported test again: counted again if its status changed, said always."""
        by_test: dict[int, list[_Finding]] = {}
        for finding in findings:
            by_test.setdefault(finding.execution.test_index, []).append(finding)
        for index, group in by_test.items():
            name, before = reported[index]
            gate = self._gates[index]
            for finding in group:
                gate.demote(finding)
            after = gate.status
            tally = (
                f" ({gate.counts[0]}/{gate.counts[1]}, min {gate.counts[2]})" if gate.counts else ""
            )
            detail = "; ".join(gate.describe(f) for f in group)
            if after is not before:
                counts[before] -= 1
                counts[after] += 1
                reported[index] = (name, after)
                verdict = "now fails" if after is Status.FAILED else "is now unverified"
                self._reporter.on_warning(f"{name}: {verdict}{tally}: {detail}")
            elif after is Status.PASSED:
                self._reporter.on_warning(
                    f"{name}: a run now fails{tally}, and the test still passes: {detail}"
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
        self._absorb(window)
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
            f"executions (context keys: {', '.join(keys)}). The context the agent writes "
            "must carry the keys of agent.correlate; the default, 'test', comes from the "
            "X-Highbar-Test header, which highbar.asgi.Middleware and "
            "hb.turn(test=<that header>) write. Run with --keep-traces to inspect the file"
        )

    def _not_observed_attached(self, window: Window, correlate: dict[str, Any]) -> str:
        """Why nothing read while an attached test ran can be counted as that test's.

        An attached agent may serve other traffic, so only a record of this test proves
        what the agent did for its message (D4).
        """
        records = [*window.tools, *window.turns]
        keys = sorted({key for record in records for key in record.context})
        if keys:
            return (
                f"not observed: no trace record in {self._collector.path} carries the keys "
                f"of agent.correlate ({describe(correlate)}); the ones read while this test "
                f"ran have context keys: {', '.join(keys)}"
            )
        if records:
            return (
                f"not observed: the {len(records)} trace records read while this test ran "
                "carry no context, so they cannot be told from other traffic on this agent; "
                f"{_tag_hint(correlate)}"
            )
        # An empty window cannot tell "no tool was called" from "the agent records nothing".
        return (
            f"not observed: nothing was written to {self._collector.path} while this "
            "test ran; the agent must write a turn per message it handles"
        )

    def _check(self, tools: list[Trace], response: str, expect: Expect) -> list[str]:
        return [
            r.message
            for assertion in self._assertions
            for r in assertion.evaluate(tools, response, expect)
            if not r.passed
        ]

    def _learn_inventory(self) -> None:
        """What the agent declared before the first test: tools register at import (D4).

        Only since its last hello: an older process's declarations in a long-lived file
        would keep a tool known after it lost its recording. Names seen in old tool
        records do not count either; seen names are this run's.
        """
        declared = self._collector.declared()
        self._inventory = set(declared) if declared is not None else None

    def _absorb(self, window: Window) -> None:
        for names in window.declarations:
            self._inventory = (self._inventory or set()) | set(names)
        self._seen.update(tool.fn for tool in window.tools)

    def _unobservable(self, expect: Expect) -> tuple[str, Expect] | None:
        """Why highbar cannot see a call the spec names, and what it can check, or None.

        The second item is the expectation without the names it cannot see (spec D4).

        A guardrail on a tool the agent never records, or on a name typed wrong, could
        never fail. Each forbidden name, and each expected call (any of its names),
        must be one the agent declared or one already seen in its records.
        """
        if self._inventory is None:
            self._undeclared.update(dict.fromkeys(expect.forbid))
            return None
        known = sorted(self._inventory | self._seen)
        groups = [(name,) for name in expect.forbid]
        groups += [
            tuple(call.name) if isinstance(call.name, list) else (call.name,)
            for call in expect.function_calls or []
        ]
        missing: list[str] = []
        hidden: set[str] = set()
        for group in groups:
            if any(name in known for name in group):
                continue
            hidden.update(group)
            listed = " or ".join(f"'{name}'" for name in group)
            close = next(
                (m[0] for name in group if (m := difflib.get_close_matches(name, known, n=1))),
                None,
            )
            missing.append(f"{listed} (did you mean '{close}'?)" if close else listed)
        if not missing:
            return None
        message = (
            f"cannot see calls to {', '.join(missing)}: the agent records "
            f"{', '.join(known) or 'no tools'}. If the agent has no such tool, remove it "
            "from the spec; if it has, record it: @hb.tool, or hb.record() with hb.declare()"
        )
        calls = [
            call
            for call in expect.function_calls or []
            if not set(call.name if isinstance(call.name, list) else [call.name]) <= hidden
        ]
        observable = replace(
            expect,
            forbid=tuple(name for name in expect.forbid if name not in hidden),
            # Entries dropped from a list must not leave `[]`, which means "no call".
            function_calls=calls if calls or expect.function_calls == [] else None,
        )
        return message, observable

    def _warn_undeclared(self) -> None:
        """Once, at the end: every forbidden name checked while nothing was declared."""
        unseen = [name for name in self._undeclared if name not in self._seen]
        if not unseen:
            return
        seen = sorted(self._seen)
        names = ", ".join(
            f"{name} (did you mean '{close[0]}'?)"
            if (close := difflib.get_close_matches(name, seen, n=1))
            else name
            for name in unseen
        )
        self._reporter.on_warning(UNDECLARED_WARNING.format(names=names))

    def _warn_once(self, template: str, correlate: Mapping[Any, Any]) -> None:
        if template in self._warned:
            return
        self._warned.add(template)
        self._reporter.on_warning(
            template.format(untagged=_untagged(correlate), hint=_tag_hint(correlate))
        )

    def _missing_file(self) -> str:
        """An attached agent's trace file is not there: no test could see a call (FR3).

        Checked before the first message: a wrong path would otherwise send every one,
        with whatever it does for real, to learn only that nothing could be observed.
        """
        return (
            f"the trace file {self._collector.path.absolute()} does not exist, so no test "
            "could see what the agent does; nothing was sent. Check agent.traces (a "
            "relative path starts where highbar runs). If the path is right, the file was "
            "removed after the agent started, or its writer creates it only with its "
            "first record: create it empty before the run"
        )

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
        """Run a single test: each of its phrasings, ``runs`` times each (D9).

        A phrasing is the trigger, one of its variations, or the whole conversation of
        a multi-turn test. The test passes only if every phrasing reaches ``min_pass``.
        """
        start = time.time()
        name = _plain_name(test)
        runs, min_pass = resolve_runs(test, self._default_runs, self._default_min_pass)

        if test.turns:
            phrasings = [("", test.turns)]
        else:
            if test.trigger is None or test.expect is None:
                raise ValueError("Single-turn test must have both 'trigger' and 'expect'")
            triggers = [test.trigger]
            if test.variations:
                paraphrases = self._paraphrases(test.trigger, test.variations)
                if isinstance(paraphrases, str):
                    return TestResult(
                        status=Status.ERROR,
                        errors=[paraphrases],
                        duration_ms=(time.time() - start) * 1000,
                    )
                triggers.extend(paraphrases)
            phrasings = [
                # The failing phrasing is the whole finding: without it the report says
                # the intent broke but not which sentence broke it. Index 0 is the
                # sentence the spec author wrote, not a generated one.
                (
                    f'[{"trigger" if index == 0 else "variation"}: "{phrasing}"] '
                    if test.variations
                    else "",
                    [Turn(trigger=phrasing, expect=test.expect)],
                )
                for index, phrasing in enumerate(triggers)
            ]

        statuses: list[Status] = []
        errors: list[str] = []
        traces: list[Trace] = []
        gate = _Gate(min_pass, counted=runs > 1 and not test.variations)
        self._gates[self._test_index] = gate
        # Every phrasing runs even after one fails: three of ten broken is a different
        # bug from one of ten, and stopping early hides which. They all go to the same
        # running agent, back to back, with no reset in between.
        for index, (label, turns) in enumerate(phrasings):
            results = [
                # Each run is its own execution, with its own test id and session.
                self._execute(
                    turns,
                    time.time(),
                    labelled=bool(test.turns),
                    name=name,
                    label=label,
                    phrasing=index,
                    run=run,
                )
                for run in range(runs)
            ]
            if index > 0:
                self._variations_run += 1
            gate.runs.append([r.status for r in results])
            statuses.append(phrasing_status(gate.runs[-1], min_pass))
            passed = sum(1 for r in results if r.passed)
            errors.extend(label + error for error in _merged_errors(results))
            if runs > 1 and test.variations and passed < min_pass:
                errors.append(f"{label}{passed}/{runs} runs passed (min {min_pass})")
            for r in results:
                traces.extend(r.traces)

        return TestResult(
            status=combine(statuses),
            errors=errors,
            duration_ms=(time.time() - start) * 1000,
            traces=traces,
            counts=gate.counts,
        )

    def _paraphrases(self, trigger: str, count: int) -> list[str] | str:
        """The generated paraphrases, or why there are none."""
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
            return f"Could not get variations: {e}"
        self._last_variation_count = len(paraphrases)
        if not paraphrases:
            # Running only the trigger would report a test of N phrasings that tested one.
            return f"no variations available (0 of {count})"
        return paraphrases

    def _execute(
        self,
        turns: list[Turn],
        start: float,
        *,
        labelled: bool,
        name: str,
        label: str = "",
        phrasing: int = 0,
        run: int = 0,
    ) -> TestResult:
        """One execution of a test: one test id and one session, shared by all its turns.

        It stops at the first turn that does not pass: later turns build on that one's
        state, so running them would test a conversation that did not happen.
        ``labelled`` prefixes each error with the turn it came from (multi-turn tests).
        """
        context = SessionContext(
            test_id=uuid.uuid4().hex, test_name=name, spec_path=self._spec_path
        )
        execution = _Execution(
            self._test_index, label, {"test": context.test_id}, phrasing=phrasing, run=run
        )
        self._executions.append(execution)
        session: dict[str, Any] = {}
        if self._session is not None:
            try:
                session = self._session.setup(context)
            except Exception as e:
                # No session, so nothing to tear down, and nothing was sent.
                return TestResult(
                    status=Status.ERROR, errors=[str(e)], duration_ms=(time.time() - start) * 1000
                )
        try:
            return self._turns(turns, start, labelled, context, session, execution)
        finally:
            if self._session is not None:
                try:
                    self._session.teardown(context, session)
                except Exception as e:
                    # The test's result stands; a session left open is worth knowing.
                    reason = str(e) if isinstance(e, SessionError) else f"{type(e).__name__}: {e}"
                    self._reporter.on_warning(f"{name}: session teardown failed: {reason}")

    def _turns(
        self,
        turns: list[Turn],
        start: float,
        labelled: bool,
        context: SessionContext,
        session: dict[str, Any],
        execution: _Execution,
    ) -> TestResult:
        values = Values(
            message=None, test_id=context.test_id, test_name=context.test_name, session=session
        )
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
            execution.correlate = dict(
                render(self._correlate, replace(values, message=resolved_trigger))
            )
        except PlaceholderError as e:
            return unverified(f"agent.correlate: {e}")
        if expect.requires_observation and any(
            other is not execution and _same_correlation(other.correlate, execution.correlate)
            for other in self._executions
        ):
            return unverified(
                f"agent.correlate gives {describe(execution.correlate)}, the same as an "
                "earlier test's: their trace records cannot be told apart. The session must "
                "give each test execution its own value"
            )

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
        self._absorb(window)

        classified = classify(window, execution.correlate)
        untagged = classified.unattributed.tools
        if untagged and classified.correlated:
            self._warn_once(UNATTRIBUTED_WARNING, execution.correlate)
        elif untagged and (self._managed or not expect.requires_observation):
            # An attached test that checks tool calls errors instead, and says why (FR2).
            self._warn_once(NO_CONTEXT_WARNING, execution.correlate)

        if not classified.correlated:
            if expect.requires_observation and not self._managed:
                # Attach mode: the agent may serve other traffic, and there is no
                # `hello` from a fresh file to rely on.
                return unverified(self._not_observed_attached(window, execution.correlate))
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

        if (unseen := self._unobservable(expect)) is not None:
            # A name highbar cannot see leaves the test unverified, unless the part of
            # the spec it can see fails on its own: a failure is a failure (combine()).
            message, observable = unseen
            real = self._check(traces, response, observable)
            if not real:
                return unverified(message)
            errors = [*real, message]

        execution.expect, execution.response = expect, response
        execution.tools, execution.passed = list(traces), not errors
        duration = (time.time() - start) * 1000
        return TestResult(
            status=Status.FAILED if errors else Status.PASSED,
            errors=errors,
            duration_ms=duration,
            traces=traces,
        )
