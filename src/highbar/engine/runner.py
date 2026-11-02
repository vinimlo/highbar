"""Test runner — orchestrates the full test execution flow."""

from __future__ import annotations

import difflib
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from highbar.adapters.file_collector import TraceFormatError
from highbar.adapters.http_client import AgentResponseError, HTTPClient
from highbar.adapters.subprocess_adapter import SubprocessAdapter
from highbar.assertions.response import truncate
from highbar.domain.models import (
    Budget,
    Counts,
    ExecutionOutcome,
    Expect,
    PhrasingOutcome,
    RunSummary,
    Spec,
    Status,
    Suite,
    SuiteOutcome,
    Test,
    TestOutcome,
    TestResult,
    ToolOutcome,
    Trace,
    Turn,
    TurnOutcome,
    TurnRecord,
    Usage,
    Window,
    combine,
    iso_utc,
    stable_key,
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
from highbar.engine.secrets import redact, redact_value
from highbar.engine.session import SessionContext, SessionError, SessionHooks
from highbar.engine.templates import TemplateError, resolve_template
from highbar.engine.usage import (
    BudgetCheck,
    add_costs,
    add_up,
    amount,
    check_budget,
    has_cost,
    over_limit,
    priced,
    turn_usage,
)

# A window with no tagged record: its untagged calls are all there is, and count.
NO_CONTEXT_WARNING = (
    "some tool calls carry {untagged}, so each counts for the test whose window it falls "
    "in, and a call that ends after the response can land in the next test; {hint}; a "
    "thread the agent starts itself needs contextvars.copy_context() to keep the tag"
)

# The same for usage: nothing ties a `turn` to its test, so the test before answers for it too.
NO_CONTEXT_USAGE_WARNING = (
    "some `turn` records with usage carry {untagged}, so each counts for the test whose window "
    "it falls in, and the test before it, which may have spent it, is an error if its budget "
    "holds only without it; {hint}; a thread the agent starts itself needs "
    "contextvars.copy_context() to keep the tag"
)

# What that test says. Nothing needs to be late: tests that pass a limit together are enough.
NO_CONTEXT_USAGE_ERROR = (
    "cannot tell which test spent the usage read in the next test's window: its `turn` records "
    "carry {untagged}, and this test keeps to its budget only without it; {hint}, and each "
    "counts for the test that spent it; a thread the agent starts itself needs "
    "contextvars.copy_context() to keep the tag"
)

# A line read after a test's window that cannot be read may be that test's late usage.
UNREADABLE_USAGE_ERROR = (
    "{error}; written after this test's window, that line may be this test's usage, so its "
    "budget cannot be verified"
)

# Next to tagged records, an untagged call counts for no test (PR 14 review I1).
UNATTRIBUTED_WARNING = (
    "some tool calls carry {untagged}, so they count for no test, and a test whose result "
    "they would change is an error; {hint}; a thread the agent starts itself needs "
    "contextvars.copy_context() to keep the tag"
)

# The same for usage: a `turn` that lost its context is in no sum, and no budget passes on it.
UNATTRIBUTED_USAGE_WARNING = (
    "some `turn` records with usage carry {untagged}, so they count for no test and the sums "
    "are incomplete, and a test whose budget they would change is an error; {hint}; a thread "
    "the agent starts itself needs contextvars.copy_context() to keep the tag"
)

UNDECLARED_WARNING = (
    "the agent has not declared the tools it records, so these guardrails cannot tell a "
    "tool it never records from one it did not call: {names}. highbar's Python SDK "
    "declares them after each hello since 0.1.0rc2; another writer adds a `tools` record "
    "(see the trace contract)"
)


@dataclass
class _TurnLog:
    """What happened to one message of an execution, for the result of the run."""

    trigger: str
    correlate: dict[str, Any] = field(default_factory=dict[str, Any])
    response: str | None = None
    response_ms: float | None = None
    tools: list[Trace] = field(default_factory=list[Trace])
    late: list[Trace] = field(default_factory=list[Trace])
    records: list[Usage] = field(default_factory=list[Usage])  # the usage of its `turn` records
    untagged: list[Usage] = field(default_factory=list[Usage])  # read next to it, no context
    suspected: list[Usage] = field(default_factory=list[Usage])  # counted for the next test
    budget: Budget | None = None

    @property
    def usage(self) -> Usage | None:
        return turn_usage(self.records, self.untagged)

    @property
    def budgeted(self) -> Usage | None:
        """``usage``, and what its budgets answer for: ``suspected`` is in no sum of this turn."""
        return turn_usage(self.records, [*self.untagged, *self.suspected])


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
    execution_id: str = ""
    started_at: float = 0.0
    duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list[str])
    turns: list[_TurnLog] = field(default_factory=list[_TurnLog])
    budget: Budget | None = None  # for the sum of its turns
    read: bool = False  # a window was read for it: a message that failed to send reads none

    def close(self, result: TestResult) -> TestResult:
        self.errors, self.duration_ms = list(result.errors), result.duration_ms
        return result


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
        traces = [self._masked(trace) for trace in result.traces]
        self._reporter.on_test_result(
            redact(test_name, self._secrets), replace(result, errors=errors, traces=traces)
        )

    def on_warning(self, message: str) -> None:
        self._reporter.on_warning(redact(message, self._secrets))

    def on_run_end(self, summary: RunSummary) -> None:
        self._reporter.on_run_end(summary)

    def _masked(self, trace: Trace) -> Trace:
        return replace(
            trace,
            fn=redact(trace.fn, self._secrets),
            args=redact_value(trace.args, self._secrets),
            result=redact_value(trace.result, self._secrets),
            error=redact_value(trace.error, self._secrets),
            context=redact_value(trace.context, self._secrets),
        )


def _cost_of(usages: Iterable[Usage | None]) -> dict[str, float]:
    totals = add_up(usages)
    return totals.cost if totals is not None else {}


def _amounts(costs: Mapping[str, float]) -> str:
    return ", ".join(f"{amount(cost)} {name}" for name, cost in sorted(costs.items()))


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
    # What each phrasing is: its kind and its text, in the order of ``runs``.
    phrasings: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])

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
        execution.errors.append(finding.error)

    def describe(self, finding: _Finding) -> str:
        execution = finding.execution
        total = len(self.runs[execution.phrasing])
        which = f" (run {execution.run + 1} of {total})" if total > 1 else ""
        return f"{execution.label}{finding.error}{which}"


@dataclass
class _Reported:
    """A test whose result went out, kept to the end: a late call can still change it."""

    suite: str
    test: Test
    name: str  # as the reporter showed it
    status: Status
    duration_ms: float
    errors: list[str]


def _counts(statuses: list[Status]) -> Counts:
    return Counts(
        passed=statuses.count(Status.PASSED),
        failed=statuses.count(Status.FAILED),
        error=statuses.count(Status.ERROR),
    )


def _suites(tests: tuple[TestOutcome, ...]) -> tuple[SuiteOutcome, ...]:
    """The sums of each suite that ran. A test's key starts with its suite's name, so two
    suites with one name are one suite in a report."""
    usages: dict[str, list[Usage | None]] = {}
    for test in tests:
        usages.setdefault(test.suite, []).extend(test.usages)
    return tuple(SuiteOutcome(name, add_up(turns)) for name, turns in usages.items())


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
        session: SessionHooks | None = None,
        spec_path: str = "",
        correlate: Mapping[str, Any] | None = None,
        secrets: Mapping[str, str] | None = None,
        default_runs: int = 1,
        max_cost: float | None = None,
        spent: Mapping[str, float] | None = None,
        spent_untagged: Mapping[str, float] | None = None,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._collector = collector
        self._assertions = assertions
        # Every message passes through it: values from ${VAR} never reach the output.
        self._reporter = _Masking(reporter, secrets) if secrets else reporter
        self._secrets: Mapping[str, str] = secrets or {}
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
        self._default_budget: Budget | None = None
        # The run's ceiling (--max-cost), what earlier specs of the run already spent, the
        # part of that with no test context, and why this one stopped because of it.
        self._max_cost = max_cost
        self._spent: Mapping[str, float] = spent or {}
        self._spent_untagged: Mapping[str, float] = spent_untagged or {}
        self._stopped: str | None = None
        # What a managed agent spent with no test context, each record once: the turns have
        # it twice, on the one it was read in and on the one it may be the late usage of.
        self._strays: list[Usage] = []
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
        self._gates: dict[int, _Gate] = {}
        self._reported: dict[int, _Reported] = {}
        self._warnings: list[str] = []
        self._run_start = time.time()
        self._test_index = 0
        # Where the next read starts: each record is read once, and none is missed
        # between two tests (a late call is often written right there).
        self._cursor = 0

    def run(self, spec: Spec) -> RunSummary:
        """Run all suites in the spec."""
        self._default_min_pass = spec.default_min_pass
        self._default_budget = spec.default_budget
        self._stopped = None
        self._strays = []
        self._warned = set()
        self._inventory, self._seen, self._undeclared = None, set(), {}
        self._executions, self._findings, self._test_index = [], [], 0
        self._gates, self._reported, self._warnings = {}, {}, []
        observes = spec.default_budget is not None or any(
            test.requires_observation for suite in spec.suites for test in suite.tests
        )
        self._run_start = time.time()

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
                    # The rest stay in self._findings until applied: an interrupt can land
                    # on any line below, and _settle must still find them.
                    self._findings = [f for f in self._findings if f not in own]
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
                    self._reported[self._test_index] = _Reported(
                        suite.name, test, name, result.status, result.duration_ms, [*result.errors]
                    )
                    self._reporter.on_test_result(name, result)
                    self._report_late(self._findings)
                    self._test_index += 1
                    self._check_ceiling(name)
                    if self._stopped is not None:
                        break
                if self._stopped is not None:
                    break
            if self._executions:
                # The last test has no next one whose read would show its late calls, and
                # one that checks only the reply still spent what a late `turn` reports.
                time.sleep(self._settle_time)
                self._read_after_last_test()
                self._report_late(self._findings)
                # That read can add to what the run spent: it may have ended over its ceiling.
                if self._stopped is None:
                    self._check_ceiling(self._reported[self._test_index - 1].name)
            self._warn_undeclared()
            self._warn_unmetered()
        finally:
            self._adapter.stop()
            self._settle()

        summary = self.snapshot()
        self._reporter.on_run_end(summary)
        return summary

    def snapshot(self) -> RunSummary:
        """Where the run stands: all of it once ``run()`` returned, or what finished.

        Every status comes from the tests' gates, never from what went to the reporter:
        a late call changes a test after ``on_test_result`` (D13). A test still running
        is left out: half a test is not a result.
        """
        tests = tuple(self._outcome(index, seen) for index, seen in self._reported.items())
        statuses = [test.status for test in tests]
        return RunSummary(
            total=len(tests),
            passed=statuses.count(Status.PASSED),
            failed=statuses.count(Status.FAILED),
            errored=statuses.count(Status.ERROR),
            duration_ms=(time.time() - self._run_start) * 1000,
            variations=sum(p.kind == "variation" for test in tests for p in test.phrasings),
            tests=tests,
            warnings=tuple(redact(message, self._secrets) for message in self._warnings),
            totals=add_up(usage for test in tests for usage in test.usages),
            suites=_suites(tests),
            stopped=self._mask(self._stopped),
            untagged_cost=_cost_of(self._masked_parts(stray) for stray in self._strays),
        )

    def _budget_of(self, test: Test) -> Budget | None:
        """What one execution of ``test`` may spend: its own budget replaces the default."""
        own = test.expect.budget if test.expect is not None else None
        return own if own is not None else self._default_budget

    def _measured(self, test: Test) -> Test:
        """``test`` as it was checked: a budget from defaults.budget counts as written in it.

        It changes what counts as right, so it belongs in ``definition_sha256``.
        """
        budget = self._budget_of(test)
        if budget is None:
            return test
        return replace(test, expect=replace(test.expect or Expect(), budget=budget))

    def _outcome(self, index: int, reported: _Reported) -> TestOutcome:
        test, gate = reported.test, self._gates.get(index)
        runs, min_pass = resolve_runs(test, self._default_runs, self._default_min_pass)
        phrasings: list[PhrasingOutcome] = []
        statuses_by_phrasing = gate.runs if gate else []
        kinds = gate.phrasings if gate else []
        for number, ((kind, text), statuses) in enumerate(
            zip(kinds, statuses_by_phrasing, strict=True)
        ):
            executions = [
                self._execution_outcome(execution, statuses[execution.run])
                for execution in self._executions
                if execution.test_index == index and execution.phrasing == number
            ]
            phrasings.append(
                PhrasingOutcome(
                    kind=kind,
                    text=self._mask(text),
                    status=phrasing_status(statuses, min_pass),
                    counts=_counts(statuses),
                    executions=tuple(executions),
                )
            )
        outcome = TestOutcome(
            id=self._mask(stable_key(reported.suite, test)),
            suite=self._mask(reported.suite),
            name=self._mask(test.plain_name),
            definition_sha256=self._measured(test).definition_sha256,
            status=gate.status if gate else reported.status,
            runs=runs,
            min_pass=min_pass,
            counts=_counts([status for run in statuses_by_phrasing for status in run]),
            duration_ms=reported.duration_ms,
            errors=self._mask(tuple(reported.errors)),
            phrasings=tuple(phrasings),
            budget=self._budget_of(test),
        )
        return replace(outcome, totals=add_up(outcome.usages))

    def _execution_outcome(self, execution: _Execution, status: Status) -> ExecutionOutcome:
        turns = tuple(
            TurnOutcome(
                trigger=self._mask(turn.trigger),
                response=self._mask(turn.response),
                response_ms=turn.response_ms,
                correlate=self._mask(turn.correlate),
                tools=(
                    *(self._tool_outcome(tool, late=False) for tool in turn.tools),
                    *(self._tool_outcome(tool, late=True) for tool in turn.late),
                ),
                usage=self._masked_usage(turn.usage),
                budget=turn.budget,
            )
            for turn in execution.turns
        )
        return ExecutionOutcome(
            execution_id=execution.execution_id,
            run=execution.run + 1,
            started_at=iso_utc(execution.started_at),
            status=status,
            duration_ms=execution.duration_ms,
            errors=self._mask(tuple(execution.errors)),
            turns=turns,
            totals=add_up(turn.usage for turn in turns),
        )

    def _tool_outcome(self, tool: Trace, *, late: bool) -> ToolOutcome:
        return ToolOutcome(
            name=self._mask(tool.fn),
            args=self._mask(tool.args),
            error=self._mask(tool.error),
            duration_ms=tool.duration_ms,
            late=late,
        )

    def _masked_usage(self, usage: Usage | None) -> Usage | None:
        return None if usage is None else self._masked_parts(usage)

    def _masked_parts(self, usage: Usage) -> Usage:
        """The agent's text masked. The counts stay numbers: they are what a reader adds."""
        return replace(
            usage,
            model=self._mask(usage.model),
            provider=self._mask(usage.provider),
            currency=self._mask(usage.currency),
            records=tuple(self._masked_parts(record) for record in usage.records),
            unattributed=tuple(self._masked_parts(stray) for stray in usage.unattributed),
        )

    def _mask(self, value: Any) -> Any:
        return redact_value(value, self._secrets)

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        self._reporter.on_warning(message)

    def _settle(self) -> None:
        """A run that stopped halfway still counts what its reads found about reported tests.

        Those findings wait for the test in progress to end, and it never will: without
        this, a test highbar knows failed would come out as passed.
        """
        self._report_late([f for f in self._findings if f.execution.test_index in self._reported])

    def _report_late(self, findings: list[_Finding]) -> None:
        """Judge a reported test again, and say so: its final state comes from the gate."""
        by_test: dict[int, list[_Finding]] = {}
        for finding in findings:
            by_test.setdefault(finding.execution.test_index, []).append(finding)
        for index, group in by_test.items():
            reported, gate = self._reported[index], self._gates[index]
            name, before = reported.name, reported.status
            for finding in group:
                gate.demote(finding)
            after = gate.status
            tally = (
                f" ({gate.counts[0]}/{gate.counts[1]}, min {gate.counts[2]})" if gate.counts else ""
            )
            detail = "; ".join(gate.describe(f) for f in group)
            reported.errors.extend(gate.describe(f) for f in group)
            if after is not before:
                reported.status = after
                verdict = "now fails" if after is Status.FAILED else "is now unverified"
                self._warn(f"{name}: {verdict}{tally}: {detail}")
            elif after is Status.PASSED:
                self._warn(f"{name}: a run now fails{tally}, and the test still passes: {detail}")
        self._findings = []

    def _last_read(self, execution: _Execution | None = None) -> _Execution | None:
        """The last execution besides ``execution`` whose window was read: what comes next
        may be its late records. One that read none already ended unverified, and stopping
        at it would let the test before it pass on records nobody checked it against."""
        return next((e for e in reversed(self._executions) if e.read and e is not execution), None)

    def _read(self) -> tuple[Window, str | None]:
        """The records written since the cursor, and why a line among them cannot be read.

        The readable ones count as any others do. A collector that read none of them
        leaves the next read to start after the line, or every read from here would stop
        on it.
        """
        try:
            return self._collector.collect(since=self._cursor), None
        except TraceFormatError as e:
            if e.window is not None:
                return e.window, str(e)
            return Window(end=self._collector.checkpoint()), str(e)

    def _read_after_last_test(self) -> None:
        last = self._last_read() or self._executions[-1]
        window, unreadable = self._read()
        self._cursor = window.end
        self._absorb(window)
        untagged = self._untagged(window.tools, last)
        self._judge_late([t for t in window.tools if t not in untagged], current=None)
        self._suspect(last, untagged)
        stray = [
            turn
            for turn in window.turns
            if attribute(turn.context, last.correlate) is Attribution.UNATTRIBUTED
        ]
        self._judge_late_usage([t for t in window.turns if t not in stray], current=None)
        self._suspect_usage(last, self._read_usage(stray), last.correlate)
        if unreadable is not None and last.passed:
            # No test comes after the last one: the line may be its late call or usage.
            last.passed = False
            self._findings.append(_Finding(last, Status.ERROR, unreadable))

    def _untagged(self, tools: list[Trace], execution: _Execution) -> list[Trace]:
        return [
            t
            for t in tools
            if attribute(t.context, execution.correlate) is Attribution.UNATTRIBUTED
        ]

    def _owner(self, context: Mapping[str, Any], current: _Execution | None) -> _Execution | None:
        """The earlier execution a late record's context names, if any."""
        for execution in reversed(self._executions):
            if execution is current:
                continue
            if attribute(context, execution.correlate) is Attribution.MINE:
                return execution
        return None

    def _judge_late(self, tools: list[Trace], current: _Execution | None) -> None:
        """Calls tagged with an earlier execution count for it, though its window closed."""
        late: dict[int, tuple[_Execution, list[Trace]]] = {}
        for tool in tools:
            owner = self._owner(tool.context, current)
            if owner is not None:
                late.setdefault(id(owner), (owner, []))[1].append(tool)
        for execution, calls in late.values():
            was_passing = execution.passed and execution.expect is not None
            execution.tools = [*execution.tools, *calls]
            if execution.turns:
                execution.turns[-1].late.extend(calls)
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

    def _judge_late_usage(self, turns: list[TurnRecord], current: _Execution | None) -> None:
        """Usage tagged with an earlier execution counts for it too, and its budgets hold again."""
        changed: dict[int, _Execution] = {}
        for turn in turns:
            owner = self._owner(turn.context, current)
            if owner is None or not owner.turns:
                # Tagged for no test of the run: a managed agent's spend is still this run's.
                if self._managed:
                    self._strays.extend(self._read_usage([turn]))
                continue
            records = self._read_usage([turn])
            if records:
                owner.turns[-1].records.extend(records)
                changed[id(owner)] = owner
        for execution in changed.values():
            check = self._budgets_of(execution)
            problems = [*check.exceeded, *check.unverified]
            if execution.passed and problems:
                execution.passed = False
                self._findings.append(
                    _Finding(
                        execution,
                        Status.FAILED if check.exceeded else Status.ERROR,
                        "a `turn` with usage was recorded after the test's window closed "
                        f"(settle_time {self._settle_time * 1000:.0f} ms): "
                        f"{'; '.join(problems)}. Raise agent.settle_time to read it in time",
                    )
                )

    def _budgets_of(self, execution: _Execution) -> BudgetCheck:
        """The limits of ``execution`` against what is known of its usage now."""
        last = execution.turns[-1]
        turn = self._budget_check(last.budget, last.budgeted)
        if execution.budget is None:
            return turn
        usages = [log.budgeted for log in execution.turns]
        whole = check_budget(execution.budget, usages, "execution")
        return BudgetCheck([*turn.exceeded, *whole.exceeded], [*turn.unverified, *whole.unverified])

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

    def _suspect_usage(
        self, execution: _Execution | None, stray: list[Usage], correlate: Mapping[Any, Any]
    ) -> None:
        """Untagged usage may be the late usage of ``execution``: if a budget of it holds
        only without that, the test is unverified."""
        if not stray:
            return
        # An attached agent may be serving others, so its cost stays out of the ceiling.
        if self._managed:
            self._strays.extend(stray)
        self._warn_once(UNATTRIBUTED_USAGE_WARNING, correlate)
        if execution is None or not execution.turns:
            return
        execution.turns[-1].untagged.extend(stray)
        check = self._budgets_of(execution)
        if execution.passed and check.unverified:
            execution.passed = False
            self._findings.append(_Finding(execution, Status.ERROR, "; ".join(check.unverified)))

    def _suspect_window(
        self, execution: _Execution | None, usage: list[Usage], correlate: Mapping[Any, Any]
    ) -> None:
        """With nothing tagged, ``usage`` counts where it was read, and may still be the late
        usage of ``execution``: if a budget of it holds only without that, the test is
        unverified, as it is when no test follows it. Its sums and the ceiling stay as they
        are, which already count that usage."""
        if not usage or execution is None or not execution.passed or not execution.turns:
            return
        execution.turns[-1].suspected.extend(usage)
        if self._budgets_of(execution).unverified:
            execution.passed = False
            message = NO_CONTEXT_USAGE_ERROR.format(
                untagged=_untagged(correlate), hint=_tag_hint(correlate)
            )
            self._findings.append(_Finding(execution, Status.ERROR, message))

    def _suspect_unreadable(self, execution: _Execution | None, error: str) -> None:
        """A line that cannot be read, after the window of ``execution``, may be its late
        usage: if it passed and has a budget, that budget is unverified. A test with no
        budget is left alone, so the line errs the test before only where usage can."""
        if execution is None or not execution.passed or not execution.turns:
            return
        if execution.budget is None and execution.turns[-1].budget is None:
            return
        execution.passed = False
        message = UNREADABLE_USAGE_ERROR.format(error=error)
        self._findings.append(_Finding(execution, Status.ERROR, message))

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
        self._warn(UNDECLARED_WARNING.format(names=names))

    def _warn_once(self, template: str, correlate: Mapping[Any, Any]) -> None:
        if template in self._warned:
            return
        self._warned.add(template)
        self._warn(template.format(untagged=_untagged(correlate), hint=_tag_hint(correlate)))

    def _costs(self) -> dict[str, float]:
        """What --max-cost counts, per currency: earlier specs included, and what a managed
        agent spent with no test context, which is in no total."""
        return add_costs(self._spent, _cost_of(self._usages()), _cost_of(self._strays))

    def _usages(self) -> list[Usage | None]:
        return [turn.usage for execution in self._executions for turn in execution.turns]

    def _check_ceiling(self, last_test: str) -> None:
        """At the end of a test: over --max-cost, the run stops here.

        A currency's sum is held against the ceiling on its own, as a budget's is: a run
        that spends in two currencies must still have one.
        """
        if self._max_cost is None:
            return
        over = over_limit(self._costs(), self._max_cost)
        if over:
            self._stopped = (
                f"--max-cost {amount(self._max_cost)}: the run spent {_amounts(over)}"
                f"{self._untagged_share(over)} by the end of '{last_test}', so it stopped "
                "there; raise the ceiling to run the rest"
            )

    def _untagged_share(self, over: Mapping[str, float]) -> str:
        """How much of ``over`` had no test context: why the totals show less than that."""
        costs = add_costs(self._spent_untagged, _cost_of(self._strays))
        shares = {name: cost for name, cost in costs.items() if name in over and cost}
        return f" ({_amounts(shares)} of it with no test context, in no total)" if shares else ""

    def _unmetered(self, usage: Usage | None) -> bool:
        """--max-cost could not count all of a turn: its own cost, or usage next to it."""
        if usage is None or not has_cost(usage):
            return True
        strays = usage.unattributed
        return any(not has_cost(stray) for stray in strays) if self._managed else bool(strays)

    def _warn_unmetered(self) -> None:
        """A ceiling that cannot see a cost could be passed in silence."""
        if self._max_cost is None:
            return
        usages = self._usages()
        missing = sum(self._unmetered(usage) for usage in usages)
        if missing:
            context = "no test context and no cost" if self._managed else "no test context"
            self._warn(
                f"--max-cost {amount(self._max_cost)} counted only the costs it knows: "
                f"{missing} of {len(usages)} turns are missing a cost and currency, or have "
                f"usage next to them with {context}, so the run may have spent more"
            )
        elif any(not has_cost(stray) for stray in self._strays):
            self._warn(
                f"--max-cost {amount(self._max_cost)} counted only the costs it knows: usage "
                "tagged for no test of the run has no cost, so the run may have spent more"
            )

    def _priced(self, usage: Usage) -> Usage:
        result, warning = priced(usage)
        if warning is not None and warning not in self._warned:
            self._warned.add(warning)
            self._warn(warning)
        return result

    def _read_usage(self, turns: list[TurnRecord]) -> list[Usage]:
        """The usage of each of ``turns`` that has any, priced. Unreadable, nothing is known."""
        return [
            Usage() if turn.usage is None else self._priced(turn.usage)
            for turn in turns
            if turn.usage is not None or turn.usage_unreadable
        ]

    def _budget_check(self, budget: Budget | None, usage: Usage | None) -> BudgetCheck:
        """The turn's own limit, against the turn's own usage."""
        return BudgetCheck() if budget is None else check_budget(budget, [usage], "turn")

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
        name = test.plain_name
        runs, min_pass = resolve_runs(test, self._default_runs, self._default_min_pass)
        budget = self._budget_of(test)

        kinds: list[tuple[str, str | None]] = [("conversation", None)]
        if test.turns:
            phrasings = [("", test.turns)]
        else:
            if test.trigger is None or test.expect is None:
                raise ValueError("Single-turn test must have both 'trigger' and 'expect'")
            # The test's budget is its execution's, checked once, on the sum of its turns.
            expect = replace(test.expect, budget=None)
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
            kinds = [
                ("trigger" if index == 0 else "variation", phrasing)
                for index, phrasing in enumerate(triggers)
            ]
            phrasings = [
                # The failing phrasing is the whole finding: without it the report says
                # the intent broke but not which sentence broke it. Index 0 is the
                # sentence the spec author wrote, not a generated one.
                (
                    f'[{"trigger" if index == 0 else "variation"}: "{phrasing}"] '
                    if test.variations
                    else "",
                    [Turn(trigger=phrasing, expect=expect)],
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
                    budget=budget,
                )
                for run in range(runs)
            ]
            gate.runs.append([r.status for r in results])
            gate.phrasings.append(kinds[index])
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
        budget: Budget | None = None,
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
            self._test_index,
            label,
            {"test": context.test_id},
            phrasing=phrasing,
            run=run,
            started_at=start,
            execution_id=context.test_id,
            budget=budget,
        )
        self._executions.append(execution)
        session: dict[str, Any] = {}
        if self._session is not None:
            try:
                session = self._session.setup(context)
            except Exception as e:
                # No session, so nothing to tear down, and nothing was sent.
                return execution.close(
                    TestResult(
                        status=Status.ERROR,
                        errors=[str(e)],
                        duration_ms=(time.time() - start) * 1000,
                    )
                )
        try:
            return execution.close(self._turns(turns, start, labelled, context, session, execution))
        finally:
            if self._session is not None:
                try:
                    self._session.teardown(context, session)
                except Exception as e:
                    # The test's result stands; a session left open is worth knowing.
                    reason = str(e) if isinstance(e, SessionError) else f"{type(e).__name__}: {e}"
                    self._warn(f"{name}: session teardown failed: {reason}")

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

        if execution.budget is not None:
            usages = [turn.usage for turn in execution.turns]
            check = check_budget(execution.budget, usages, "execution")
            all_errors.extend(check.exceeded)
            if check.exceeded:
                statuses.append(Status.FAILED)
            elif check.unverified and all(status is Status.PASSED for status in statuses):
                # Next to a turn that ended otherwise, it would only repeat the cause: a
                # conversation that stopped early has no usage for the turns it skipped.
                all_errors.extend(check.unverified)
                statuses.append(Status.ERROR)
            execution.passed = execution.passed and not (check.exceeded or check.unverified)

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
        log = _TurnLog(trigger=trigger, budget=expect.budget)
        execution.turns.append(log)
        # A budget on the whole execution reads the usage of each turn, whatever it expects.
        observes = expect.requires_observation or execution.budget is not None

        def unverified(error: str) -> TestResult:
            duration = (time.time() - start) * 1000
            return TestResult(status=Status.ERROR, errors=[error], duration_ms=duration)

        try:
            resolved_trigger = resolve_template(trigger, previous_traces)
        except TemplateError as e:
            # Sending the raw "{{...}}" would test a message nobody meant to write; the
            # turn is unverified, and the rest of the run goes on.
            return unverified(str(e))
        log.trigger = resolved_trigger

        try:
            execution.correlate = dict(
                render(self._correlate, replace(values, message=resolved_trigger))
            )
        except PlaceholderError as e:
            return unverified(f"agent.correlate: {e}")
        log.correlate = dict(execution.correlate)
        if observes and any(
            other is not execution and _same_correlation(other.correlate, execution.correlate)
            for other in self._executions
        ):
            return unverified(
                f"agent.correlate gives {describe(execution.correlate)}, the same as an "
                "earlier test's: their trace records cannot be told apart. The session must "
                "give each test execution its own value"
            )

        sent_at = time.time()
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
        log.response, log.response_ms = response, (time.time() - sent_at) * 1000

        time.sleep(self._settle_time)
        window, unreadable = self._read()
        self._cursor = window.end
        execution.read = True
        self._absorb(window)
        previous = self._last_read(execution)
        result = self._verdict(
            window, response, expect, execution, log, previous, start, observes=observes
        )
        if unreadable is None:
            return result
        # What could be read counted as it does in any window. The line itself may be
        # the call this test is about, or the late usage of the test before it.
        execution.passed = False
        self._suspect_unreadable(previous, unreadable)
        return replace(result, status=Status.ERROR, errors=[unreadable])

    def _verdict(
        self,
        window: Window,
        response: str,
        expect: Expect,
        execution: _Execution,
        log: _TurnLog,
        previous: _Execution | None,
        start: float,
        *,
        observes: bool,
    ) -> TestResult:
        """What ``window`` says of this turn: its records counted, and it checked against them."""

        def unverified(error: str) -> TestResult:
            duration = (time.time() - start) * 1000
            return TestResult(status=Status.ERROR, errors=[error], duration_ms=duration)

        classified = classify(window, execution.correlate)
        untagged = classified.unattributed.tools
        if untagged and classified.correlated:
            self._warn_once(UNATTRIBUTED_WARNING, execution.correlate)
        elif untagged and (self._managed or not observes):
            # An attached test that checks tool calls errors instead, and says why (FR2).
            self._warn_once(NO_CONTEXT_WARNING, execution.correlate)

        if not classified.correlated:
            stray = self._read_usage(window.turns)
            self._suspect_window(previous, stray, execution.correlate)
            if observes and not self._managed:
                # Attach mode: the agent may serve other traffic, and there is no
                # `hello` from a fresh file to rely on.
                return unverified(self._not_observed_attached(window, execution.correlate))
            # Nothing in the window is tagged: it is all there is to go by. An attached
            # agent may be serving others, and their tokens are not this test's.
            traces = window.tools
            log.tools = list(traces)
            log.records = stray if self._managed else []
            if log.records:
                self._warn_once(NO_CONTEXT_USAGE_WARNING, execution.correlate)
            errors = self._check(traces, response, expect)
        else:
            self._judge_late(classified.other.tools, current=execution)
            self._judge_late_usage(classified.other.turns, current=execution)
            self._suspect(previous, untagged)
            log.untagged = self._read_usage(classified.unattributed.turns)
            self._suspect_usage(previous, log.untagged, execution.correlate)
            if observes and not (classified.mine.tools or classified.mine.turns):
                return unverified(self._not_observed(classified, execution.correlate))
            traces = classified.mine.tools
            log.tools = list(traces)
            log.records = self._read_usage(classified.mine.turns)
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

        budget = self._budget_check(expect.budget, log.usage)
        if (unseen := self._unobservable(expect)) is not None:
            # A name highbar cannot see leaves the test unverified, unless the part of
            # the spec it can see fails on its own: a failure is a failure (combine()).
            message, observable = unseen
            real = [*self._check(traces, response, observable), *budget.exceeded]
            if not real:
                return unverified(message)
            errors = [*real, message]
        else:
            errors = [*errors, *budget.exceeded]

        # The same for a budget: without usage nothing is known about it, and that only
        # leaves the test unverified when nothing else already failed it.
        if budget.unverified:
            if not errors:
                return unverified("; ".join(budget.unverified))
            errors = [*errors, *budget.unverified]

        execution.expect, execution.response = expect, response
        execution.tools, execution.passed = list(traces), not errors
        duration = (time.time() - start) * 1000
        return TestResult(
            status=Status.FAILED if errors else Status.PASSED,
            errors=errors,
            duration_ms=duration,
            traces=traces,
        )
