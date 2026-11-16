"""Test runner — orchestrates the full test execution flow."""

from __future__ import annotations

import difflib
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

from highbar.adapters import file_collector, subprocess_adapter
from highbar.adapters.file_collector import TraceFormatError
from highbar.adapters.http_client import AgentResponseError, HTTPClient, InfraCause
from highbar.adapters.subprocess_adapter import SubprocessAdapter
from highbar.assertions.function_calls import FunctionCallsAssertion, unrecorded_returns
from highbar.assertions.llm import check_llm
from highbar.assertions.records import RecordNotObserved
from highbar.assertions.response import truncate
from highbar.assertions.retrieval import check_retrieval
from highbar.domain.models import (
    BUDGET_EXCEEDED,
    REPEATED_ERRORS,
    Budget,
    Counts,
    ExecutionOutcome,
    Expect,
    LlmCall,
    PhrasingOutcome,
    Retrieval,
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
from highbar.engine import observation, signals
from highbar.engine import session as sessions
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
from highbar.engine.runs import against_expected, phrasing_status, resolve_runs
from highbar.engine.secrets import redact, redact_value
from highbar.engine.session import SessionContext, SessionError, SessionHooks
from highbar.engine.streak import ErrorStreak, stop_message
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

# With --jobs, a record names its test or it counts for none (HB-8): several tests are
# in flight at once, so the window a record falls in says nothing about whose it is.
JOBS_NOT_OBSERVED = (
    "not observed: with --jobs, every record must carry agent.correlate (untagged records "
    "cannot be told apart); {hint}, or run with --jobs 1"
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
    turns: list[TurnRecord] = field(default_factory=list[TurnRecord])  # those records (HB-12)
    unobserved: str | None = None  # why expect.check cannot read them: no record of its own
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
class _Held:
    """What other reads took in for an execution in flight, with --jobs (HB-8).

    ``tagged``: its own records, counted in its next window as if it had read them.
    ``untagged``: records with no context, which may be its own as well as the reader's.
    ``unreadable``: lines that could not be read, which may be its own record.
    """

    tagged: Window = field(default_factory=Window)
    untagged: Window = field(default_factory=Window)
    unreadable: list[str] = field(default_factory=list[str])


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
    # Its records of the types beyond tool calls (HB-11, HB-3), as late ones add to them.
    llms: list[LlmCall] = field(default_factory=list[LlmCall])
    retrievals: list[Retrieval] = field(default_factory=list[Retrieval])
    passed: bool = False
    # No record of its own in a managed run: it rests on the agent highbar started being
    # the one that answered, which it may not be once that agent is gone (HB-43).
    on_faith: bool = False
    execution_id: str = ""
    started_at: float = 0.0
    duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list[str])
    turns: list[_TurnLog] = field(default_factory=list[_TurnLog])
    budget: Budget | None = None  # for the sum of its turns
    read: bool = False  # a window was read for it: a message that failed to send reads none
    # Why the send of its last turn failed, when the transport or HTTP did (HB-34).
    cause: InfraCause | None = None
    # With --jobs, from its first message until it ends: other reads keep its records here.
    held: _Held | None = None

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


def _quoted(names: list[str]) -> str:
    return " and ".join(f"'{name}'" for name in names)


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


def _any_record(*windows: Window) -> bool:
    return any(w.tools or w.turns or w.llms or w.retrievals for w in windows)


def _merged_errors(results: list[TestResult], conditions: Mapping[str, str]) -> list[str]:
    """The errors of a phrasing's runs, one line per condition (HB-27).

    A response check's message ends with the agent's reply, which an LLM changes on every
    run. Grouped by what the message says apart from it (``conditions``), a condition is
    one line that says in how many runs it failed, with the reply of the first of them.
    The same text in each run keeps its form. Each execution keeps its own message.
    """
    if len(results) == 1:
        return list(results[0].errors)
    groups: dict[str, list[tuple[int, str]]] = {}
    for run, result in enumerate(results, start=1):
        mine: dict[str, str] = {}
        for error in result.errors:
            mine.setdefault(conditions.get(error, error), error)
        for key, error in mine.items():
            groups.setdefault(key, []).append((run, error))
    lines: list[str] = []
    for failed in groups.values():
        (run, first), count = failed[0], f"{len(failed)} of {len(results)} runs"
        same = all(error == first for _, error in failed)
        lines.append(f"{first} ({count})" if same else f"{first} ({count}; as in run {run})")
    return lines


@dataclass
class _Gate:
    """The status of each run of a test, per phrasing, kept for the whole run.

    A late call fails its own run, and the test is judged again against min_pass:
    a run that fails late counts like a run that fails in time, no more.

    A test the run stopped in the middle of (``interrupted``, HB-34) is no pass: half a
    test is not a verdict, whatever its runs say. It is an error unless a failure is
    already known, which wins as in ``combine`` (his answer, "Falhou", 15/11 13:11, in
    #25's thread 4526841100): a phrasing that ended failed, or one the stop cut whose
    failures alone already keep it under ``min_pass``.
    """

    min_pass: int
    counted: bool  # the title shows passed/runs: no variations, more than one run
    runs: list[list[Status]] = field(default_factory=list[list[Status]])
    # What each phrasing is: its kind and its text, in the order of ``runs``.
    phrasings: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])
    each: int = 1  # the runs each phrasing has to make
    interrupted: bool = False

    @property
    def status(self) -> Status:
        phrasings = [self.phrasing(number) for number in range(len(self.runs))]
        if self.interrupted:
            # What the stop left out is unverified: never a pass, and never over a failure.
            phrasings.append(Status.ERROR)
        return combine(phrasings)

    def phrasing(self, number: int) -> Status:
        """The status of one phrasing. One the run stopped before its last run is failed
        only when its failures alone already keep it under min_pass, the runs it never
        made counted as ones that could have passed; otherwise it is an error."""
        statuses = self.runs[number]
        if self.interrupted and len(statuses) < self.each:
            missing = [Status.ERROR] * (self.each - len(statuses))
            if phrasing_status([*statuses, *missing], self.min_pass) is Status.FAILED:
                return Status.FAILED
            return Status.ERROR
        return phrasing_status(statuses, self.min_pass)

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


# What a test that a late call judged again now is, in the warning that says so.
_VERDICTS = {
    Status.PASSED: "now passes",
    Status.FAILED: "now fails",
    Status.ERROR: "is now unverified",
}


def _judged(test: Test, result: TestResult) -> TestResult:
    """``result`` as the reporter shows it: judged against the test's expect_status."""
    status, mismatch = against_expected(result.status, test.expect_status)
    errors = [mismatch, *result.errors] if mismatch else result.errors
    return replace(result, status=status, errors=errors, expected=test.expect_status)


@dataclass
class _Reported:
    """A test whose result went out, kept to the end: a late call can still change it."""

    suite: str
    test: Test
    name: str  # as the reporter showed it
    status: Status  # what happened, before expect_status judges it
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


def _record_checks(
    expect: Expect, llms: list[LlmCall], retrievals: list[Retrieval]
) -> tuple[list[str], list[str]]:
    """The checks of the record types beyond tool calls: their failures, and what could
    not be checked for want of a record (HB-11, HB-3; decision E1)."""
    failures: list[str] = []
    unseen: list[str] = []
    if expect.llm is not None:
        try:
            failures += check_llm(llms, expect.llm)
        except RecordNotObserved as e:
            unseen.append(str(e))
    if expect.retrieval is not None:
        try:
            failures += check_retrieval(retrievals, expect.retrieval)
        except RecordNotObserved as e:
            unseen.append(str(e))
    return failures, unseen


def _test_name(test: Test, variations_run: int | None = None) -> str:
    """Display name. ``variations_run`` is how many paraphrases were actually executed.

    The title reports that number, not the one the spec asked for: when the generator
    produced 3 of the 5 requested, saying "5 variations" overstates what was tested.
    """
    if test.turns:
        label = test.name or f"multi-turn ({len(test.turns)} turns)"
    else:
        label = test.name or test.trigger or ""
    if test.variations:
        if variations_run is None or variations_run == test.variations:
            return f"{label} ({test.variations} variations)"
        return f"{label} ({variations_run} of {test.variations} variations)"
    return label


def _turn_label(
    turn: Turn,
    previous_traces: list[Trace],
    responses: list[str | None],
    session: Mapping[str, Any],
) -> str:
    """The turn's ``name``, or the message it sends, cut at 80 characters."""
    if turn.name:
        return turn.name
    try:
        trigger = resolve_template(turn.trigger, previous_traces, responses, session)
    except TemplateError:
        trigger = turn.trigger
    return truncate(trigger, 80)


class _PerTest(threading.local):
    """What the test a thread runs now is: one per worker with --jobs (HB-8)."""

    def __init__(self) -> None:
        self.test_index = 0
        self.args: Mapping[str, Any] = {}
        self.history: Sequence[Mapping[str, str]] = ()
        self.variation_count: int | None = None
        self.execution: _Execution | None = None  # the last one it started


@dataclass(frozen=True)
class _Ceiling:
    """Where --max-cost was passed with --jobs: tests still in flight then finish (D4)."""

    test: str  # the test whose end passed it
    spent: str  # what the run had spent then, as the message says it
    in_flight: int


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
        hook_timeout: float | None = None,
        spent: Mapping[str, float] | None = None,
        spent_untagged: Mapping[str, float] | None = None,
        max_errors: int | None = None,
        streak: ErrorStreak | None = None,
        jobs: int = 1,
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
        # agent.timeout in seconds, for the line said while a signal waits (HB-44).
        self._hook_timeout = hook_timeout
        self._spent: Mapping[str, float] = spent or {}
        self._spent_untagged: Mapping[str, float] = spent_untagged or {}
        self._stopped: str | None = None
        self._stop_reason: str | None = None  # BUDGET_EXCEEDED or REPEATED_ERRORS
        # --max-errors (None or 0: off), and the errors in a row so far, which the specs
        # before this one may have started: the count goes on across them (HB-34).
        self._max_errors = max_errors
        self._streak = streak if streak is not None else ErrorStreak()
        # --jobs: how many tests may be in flight at once (HB-8). 1 is the run as it was.
        self._jobs = jobs
        # What a managed agent spent with no test context, each record once: the turns have
        # it twice, on the one it was read in and on the one it may be the late usage of.
        self._strays: list[Usage] = []
        # The state of the test in progress, one per thread: with --jobs, one per worker.
        self._local = _PerTest()
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
        # The agent highbar started was seen gone (HB-43): why, once something else
        # answers on its port; and whether a crash with nothing there was told.
        self._agent_gone: str | None = None
        self._crash_told = False
        self._run_start = time.time()
        # What agent.run.setup returned (HB-10): {{run.*}} in every request and session.
        self._run_values: Mapping[str, Any] = {}
        # What each failure message says apart from the agent's reply (HB-27): a
        # phrasing's runs that fail the same condition with other replies are one line.
        self._conditions: dict[str, str] = {}
        # Where the next read starts: each record is read once, and none is missed
        # between two tests (a late call is often written right there).
        self._cursor = 0
        # --jobs (HB-8): one lock for everything but the send and the wait, the tests in
        # spec order, and where the run of them stands.
        self._lock = threading.Lock()
        self._plan: list[tuple[int, Suite, Test]] = []
        self._suites: list[Suite] = []
        self._suites_told = 0
        self._next = 0
        self._in_flight: set[int] = set()
        self._done: dict[int, tuple[str, TestResult]] = {}
        self._flushed = 0
        self._ceiling: _Ceiling | None = None
        self._interrupting = False
        self._crash: BaseException | None = None

    # The state of the test in progress lives in self._local: with --jobs, each worker
    # runs its own test.
    @property
    def _test_index(self) -> int:
        return self._local.test_index

    @_test_index.setter
    def _test_index(self, value: int) -> None:
        self._local.test_index = value

    @property
    def _args(self) -> Mapping[str, Any]:
        """The session.args of the test that runs now (HB-9), for each of its executions."""
        return self._local.args

    @_args.setter
    def _args(self, value: Mapping[str, Any]) -> None:
        self._local.args = value

    @property
    def _history(self) -> Sequence[Mapping[str, str]]:
        return self._local.history

    @_history.setter
    def _history(self, value: Sequence[Mapping[str, str]]) -> None:
        self._local.history = value

    @property
    def _last_variation_count(self) -> int | None:
        return self._local.variation_count

    @_last_variation_count.setter
    def _last_variation_count(self, value: int | None) -> None:
        self._local.variation_count = value

    @property
    def _parallel(self) -> bool:
        return self._jobs > 1

    @contextmanager
    def _unlocked(self) -> Iterator[None]:
        """With --jobs, the send and the wait run without the lock: the rest waits for it."""
        if not self._parallel:
            yield
            return
        self._lock.release()
        try:
            yield
        finally:
            self._lock.acquire()

    def run(self, spec: Spec) -> RunSummary:
        """Run all suites in the spec."""
        self._default_min_pass = spec.default_min_pass
        self._default_budget = spec.default_budget
        self._stopped, self._stop_reason = None, None
        self._strays = []
        self._warned = set()
        self._inventory, self._seen, self._undeclared = None, set(), {}
        self._executions, self._findings, self._test_index = [], [], 0
        self._gates, self._reported, self._warnings = {}, {}, []
        self._ceiling, self._interrupting, self._crash = None, False, None
        observes = spec.default_budget is not None or any(
            test.requires_observation for suite in spec.suites for test in suite.tests
        )
        self._run_start = time.time()

        # agent.run (HB-10): its setup before the agent starts, and its teardown on every
        # way out once the setup finished, an agent that does not start included.
        self._run_values = {}
        if self._session is not None:
            self._run_values = self._session.before_run()
        try:
            self._adapter.start()
        except BaseException:
            self._after_run()
            raise
        try:
            if self._managed and observes:
                self._await_hello()
            elif observes and self._collector.missing():
                raise AgentNotObservedError(self._missing_file())
            self._cursor = self._collector.checkpoint()
            if observes:
                self._learn_inventory()
            if self._parallel:
                self._run_parallel(spec)
            else:
                self._run_in_order(spec)
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
            try:
                self._adapter.stop()
                self._settle()
            finally:
                # A second Ctrl-C can land while the agent stops (review of #54): what
                # the setup changed is put back even then.
                self._after_run()

        summary = self.snapshot()
        self._reporter.on_run_end(summary)
        return summary

    def _run_in_order(self, spec: Spec) -> None:
        """Each test after the one before it, as --jobs 1 runs them."""
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
                result = self._with_findings(self._test_index, result, own)
                name = _test_name(test, self._last_variation_count)
                self._reported[self._test_index] = _Reported(
                    suite.name, test, name, result.status, result.duration_ms, [*result.errors]
                )
                self._reporter.on_test_result(name, _judged(test, result))
                self._report_late(self._findings)
                self._test_index += 1
                # A stop for repeated errors came first, in the test: it is the reason.
                if self._stopped is None:
                    self._check_ceiling(name)
                if self._stopped is not None:
                    return

    def _run_parallel(self, spec: Spec) -> None:
        """Up to --jobs tests in flight at once, started in spec order (HB-8).

        Each worker runs whole tests, holding the lock but for the send and the wait. The
        results reach the reporter in spec order. A stop (--max-cost, --max-errors) starts
        no more tests; Ctrl-C does the same, then waits for the sends in flight, within
        agent.timeout. The workers are daemon threads: the signal handlers stay here.
        """
        self._suites = list(spec.suites)
        self._plan = [
            (number, suite, test)
            for number, suite in enumerate(spec.suites)
            for test in suite.tests
        ]
        self._suites_told, self._next, self._flushed = 0, 0, 0
        self._in_flight, self._done = set(), {}
        workers = [
            threading.Thread(target=self._work, name=f"highbar-job-{n + 1}", daemon=True)
            for n in range(min(self._jobs, len(self._plan)))
        ]
        try:
            for worker in workers:
                worker.start()
            # Joined without the lock: a signal that lands here leaves it as it was.
            while any(worker.is_alive() for worker in workers):
                for worker in workers:
                    worker.join(0.1)
        except BaseException:
            self._stop_workers(workers)
            raise
        if self._crash is not None:
            raise self._crash
        if self._stopped is None and self._ceiling is None:
            # Suites with no test are told too, as a run in order tells them.
            self._tell_suites(len(self._suites) - 1)
        self._test_index = self._flushed

    def _work(self) -> None:
        """One worker: the next test of the plan, until there is none or the run stops."""
        with self._lock:
            try:
                while (index := self._dispatch()) is not None:
                    _, _, test = self._plan[index]
                    self._test_index = index
                    self._last_variation_count = None
                    result = self._run_test(test)
                    self._in_flight.discard(index)
                    name = _test_name(test, self._last_variation_count)
                    self._done[index] = (name, result)
                    self._note_ceiling(name)
                    self._flush()
            except BaseException as e:
                # Told on the main thread, after the other workers end.
                if self._crash is None:
                    self._crash = e

    def _dispatch(self) -> int | None:
        """The next test to start, or None: none left, or the run stops."""
        stopping = (
            self._stopped is not None
            or self._ceiling is not None
            or self._interrupting
            or self._crash is not None
        )
        if stopping or self._next >= len(self._plan):
            return None
        index, self._next = self._next, self._next + 1
        self._in_flight.add(index)
        return index

    def _note_ceiling(self, name: str) -> None:
        """--max-cost with --jobs: past it, no test starts, and the ones in flight end (D4)."""
        if self._max_cost is None or self._ceiling is not None or self._stopped is not None:
            return
        over = over_limit(self._costs(), self._max_cost)
        if over:
            spent = f"{_amounts(over)}{self._untagged_share(over)}"
            self._ceiling = _Ceiling(name, spent, len(self._in_flight))

    def _flush(self) -> None:
        """Tell the reporter each finished test whose tests before it were told."""
        while self._flushed in self._done:
            self._tell(self._flushed)
            self._flushed += 1
        self._report_reported()

    def _tell(self, index: int) -> None:
        number, suite, test = self._plan[index]
        self._tell_suites(number)
        name, result = self._done.pop(index)
        own = [f for f in self._findings if f.execution.test_index == index]
        self._findings = [f for f in self._findings if f not in own]
        result = self._with_findings(index, result, own)
        self._reported[index] = _Reported(
            suite.name, test, name, result.status, result.duration_ms, [*result.errors]
        )
        self._reporter.on_test_result(name, _judged(test, result))

    def _tell_suites(self, number: int) -> None:
        while self._suites_told <= number:
            self._reporter.on_suite_start(self._suites[self._suites_told])
            self._suites_told += 1

    def _report_reported(self) -> None:
        """What reads found about told tests; the rest waits for its test to be told."""
        told = [f for f in self._findings if f.execution.test_index in self._reported]
        rest = [f for f in self._findings if f.execution.test_index not in self._reported]
        if told:
            self._report_late(told)
        self._findings = rest

    def _stop_workers(self, workers: list[threading.Thread]) -> None:
        """A signal: no test starts, the sends in flight get agent.timeout to end, and the
        tests that ended with a verdict are told. The lock is kept from then on: a worker
        still waiting for its reply changes nothing after this."""
        self._interrupting = True
        seconds = self._hook_timeout if self._hook_timeout is not None else 30.0
        flying = len(self._in_flight)
        if flying:
            tests = "1 test" if flying == 1 else f"{flying} tests"
            signals.say(
                f"Waiting for the {tests} in flight to get a reply (at most {seconds:g}s, "
                "agent.timeout), then stopping."
            )
        deadline = time.monotonic() + seconds
        try:
            for worker in workers:
                worker.join(max(0.0, deadline - time.monotonic()))
            sealed = self._lock.acquire(timeout=max(0.1, deadline - time.monotonic()))
        except BaseException:
            # A second signal: stop waiting.
            return
        if not sealed:
            return
        for index in sorted(self._done):
            gate = self._gates.get(index)
            if gate is None or not gate.interrupted:
                self._tell(index)
        self._report_reported()

    def _with_findings(self, index: int, result: TestResult, own: list[_Finding]) -> TestResult:
        """``result`` with what reads found about its own executions before it was told."""
        if not own:
            return result
        gate = self._gates[index]
        for finding in own:
            gate.demote(finding)
        return replace(
            result,
            status=gate.status,
            counts=gate.counts,
            errors=[*result.errors, *(gate.describe(f) for f in own)],
        )

    def snapshot(self) -> RunSummary:
        """Where the run stands: all of it once ``run()`` returned, or what finished.

        Every status comes from the tests' gates, never from what went to the reporter:
        a late call changes a test after ``on_test_result`` (D13). A test still running
        is left out: half a test is not a result. A test that repeated errors stopped
        halfway is in, as an error with the executions it had (HB-34).
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
            stop_reason=self._stop_reason,
        )

    @property
    def interrupted(self) -> bool:
        """The run stopped in the middle of a test, so not every test ran all it had to."""
        return any(gate.interrupted for gate in self._gates.values())

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
                    status=gate.phrasing(number) if gate else phrasing_status(statuses, min_pass),
                    counts=_counts(statuses),
                    executions=tuple(executions),
                )
            )
        observed = gate.status if gate else reported.status
        status, mismatch = against_expected(observed, test.expect_status)
        errors = (mismatch, *reported.errors) if mismatch else tuple(reported.errors)
        outcome = TestOutcome(
            id=self._mask(stable_key(reported.suite, test)),
            suite=self._mask(reported.suite),
            name=self._mask(test.plain_name),
            definition_sha256=self._measured(test).definition_sha256,
            status=status,
            runs=runs,
            min_pass=min_pass,
            counts=_counts([status for run in statuses_by_phrasing for status in run]),
            duration_ms=reported.duration_ms,
            errors=self._mask(errors),
            phrasings=tuple(phrasings),
            budget=self._budget_of(test),
            expect_status=test.expect_status,
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

    def _after_run(self) -> None:
        """agent.run's teardown. Its failure is a warning: the results stand, but what the
        setup changed may still be as the run left it.

        A signal that lands while it runs waits for its end (within agent.timeout), then
        goes where it would have gone: a Ctrl-C there would leave the setup's change in
        place. The warning is told first.
        """
        if self._session is None:
            return
        within = (
            f"at most {self._hook_timeout:g}s, agent.timeout"
            if self._hook_timeout is not None
            else "within agent.timeout"
        )
        notice = (
            "Waiting for agent.run.teardown to put back what agent.run.setup changed "
            f"({within}), then stopping."
        )
        with signals.holding_signals(notice):
            try:
                self._session.after_run()
            except Exception as e:
                reason = str(e) if isinstance(e, SessionError) else f"{type(e).__name__}: {e}"
                self._warn(f"{reason}; check by hand what it was to undo")

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
            reported.status = after
            # The verdict is the test's, judged against its expect_status (HB-21): a late
            # call that fails a negative control can make it pass.
            expected = reported.test.expect_status
            was, now = against_expected(before, expected)[0], against_expected(after, expected)[0]
            ends = f" (it ends {after}, and expect_status is {expected})" if expected else ""
            if now is not was:
                self._warn(f"{name}: {_VERDICTS[now]}{ends}{tally}: {detail}")
            elif now is Status.PASSED or after is Status.PASSED:
                still = "passes" if now is Status.PASSED else "fails"
                self._warn(
                    f"{name}: a run now fails{tally}, and the test still {still}{ends}: {detail}"
                )
        self._findings = []

    def _last_read(self, execution: _Execution | None = None) -> _Execution | None:
        """The last execution besides ``execution`` whose window was read: what comes next
        may be its late records. One that read none already ended unverified, and stopping
        at it would let the test before it pass on records nobody checked it against."""
        return next(
            (
                e
                for e in reversed(self._executions)
                # With --jobs, one still in flight gets the records it may own in its window.
                if e.read and e is not execution and e.held is None
            ),
            None,
        )

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

    def _agent_exit(self) -> subprocess_adapter.AgentExit | None:
        """How the agent highbar started ended, or None while it runs, or when attached."""
        if not self._managed:
            return None
        ended = self._adapter.exited()
        return ended if isinstance(ended, subprocess_adapter.AgentExit) else None

    @staticmethod
    def _exit_text(ended: subprocess_adapter.AgentExit) -> str:
        cleanly = (
            " (cleanly: `start` may launch the agent in the background)" if ended.code == 0 else ""
        )
        return f"the agent highbar started exited with code {ended.code}{cleanly}"

    def _look_at_agent(self, current: _Execution | None = None) -> str | None:
        """Why an execution with no record of its own is unverified now, or None (HB-43).

        Gone with something else on its port, the agent may have died before any message:
        that process answered them, and when the agent died cannot be told. So every
        execution that rested on it is unverified, the ones already judged through a
        finding. Gone with nothing on its port, it crashed, and what it answered stands.
        ``current`` is the execution whose turn is being judged: its own result says so.
        """
        if self._agent_gone is not None:
            return self._agent_gone
        ended = self._agent_exit()
        if ended is None:
            return None
        if not ended.port_answers:
            if not self._crash_told:
                self._crash_told = True
                self._warn(f"{self._exit_text(ended)} during the run; see {self._adapter.log_path}")
            return None
        self._agent_gone = (
            f"{self._exit_text(ended)}, and another process answers on its port: this "
            "execution has no trace record of its own, so what answered it may not be the "
            f"agent. See {self._adapter.log_path}"
        )
        for execution in self._executions:
            if execution is not current and execution.on_faith and execution.passed:
                execution.passed = False
                self._findings.append(_Finding(execution, Status.ERROR, self._agent_gone))
        return self._agent_gone

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
        self._judge_late_records(window.llms, window.retrievals, current=None)
        self._suspect_usage(last, self._read_usage(stray), last.correlate)
        if unreadable is not None and last.passed:
            # No test comes after the last one: the line may be its late call or usage.
            last.passed = False
            self._findings.append(_Finding(last, Status.ERROR, unreadable))
        # The last look at the agent: gone by now, it may have been gone all along.
        self._look_at_agent()

    def _distribute(
        self, window: Window, reader: _Execution, unreadable: str | None
    ) -> tuple[Window, Window, str | None]:
        """--jobs: the records of a read, each for the execution it names (HB-8).

        A record of another execution in flight is kept for it, as if it had read it.
        One with no context could be any of theirs: it stays in this window and is
        shared with each of them, and so is a line that cannot be read. Returns this
        execution's window, what the reads before shared with it, and why a line it
        read, or was shared, cannot be read.
        """
        flying = [e for e in self._executions if e.held is not None and e is not reader]
        held = reader.held or _Held()
        reader.held = _Held()  # emptied: what comes from now on waits for its next read
        own = held.tagged
        mine = Window(
            tools=[*own.tools],
            turns=[*own.turns],
            llms=[*own.llms],
            retrievals=[*own.retrievals],
            hellos=window.hellos,
            declarations=window.declarations,
            end=window.end,
        )
        parts: list[tuple[list[Any], Callable[[Window], list[Any]]]] = [
            (window.tools, lambda w: w.tools),
            (window.turns, lambda w: w.turns),
            (window.llms, lambda w: w.llms),
            (window.retrievals, lambda w: w.retrievals),
        ]
        for records, kind in parts:
            for record in records:
                context: Mapping[str, Any] = record.context
                owner = next(
                    (e for e in flying if attribute(context, e.correlate) is Attribution.MINE),
                    None,
                )
                if owner is not None:
                    assert owner.held is not None
                    kind(owner.held.tagged).append(record)
                    continue
                kind(mine).append(record)
                if attribute(context, reader.correlate) is not Attribution.UNATTRIBUTED:
                    continue
                for other in flying:
                    if attribute(context, other.correlate) is Attribution.UNATTRIBUTED:
                        assert other.held is not None
                        kind(other.held.untagged).append(record)
        if unreadable is not None:
            for other in flying:
                assert other.held is not None
                other.held.unreadable.append(unreadable)
        return mine, held.untagged, unreadable or next(iter(held.unreadable), None)

    def _release(self, execution: _Execution) -> None:
        """--jobs: an execution ends. What other reads kept for it since its last read
        counts as late, as it would in the read after its window (HB-8)."""
        held, execution.held = execution.held, None
        if held is None:
            return
        tagged = held.tagged
        self._judge_late(tagged.tools, current=None)
        self._judge_late_usage(tagged.turns, current=None)
        self._judge_late_records(tagged.llms, tagged.retrievals, current=None)
        untagged = classify(held.untagged, execution.correlate).unattributed
        self._suspect(execution, untagged.tools)
        stray = self._read_usage(untagged.turns)
        if stray and execution.turns:
            execution.turns[-1].untagged.extend(stray)
            check = self._budgets_of(execution)
            if execution.passed and check.unverified:
                execution.passed = False
                self._findings.append(
                    _Finding(execution, Status.ERROR, "; ".join(check.unverified))
                )
        for error in held.unreadable[:1]:
            self._suspect_unreadable(execution, error)

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

    def _judge_late_records(
        self, llms: list[LlmCall], retrievals: list[Retrieval], current: _Execution | None
    ) -> None:
        """Records beyond tool calls, tagged with an earlier execution, count for it (E1)."""
        late: dict[int, tuple[_Execution, int]] = {}
        for call in llms:
            owner = self._owner(call.context, current)
            if owner is not None:
                owner.llms = [*owner.llms, call]
                late[id(owner)] = (owner, late.get(id(owner), (owner, 0))[1] + 1)
        for record in retrievals:
            owner = self._owner(record.context, current)
            if owner is not None:
                owner.retrievals = [*owner.retrievals, record]
                late[id(owner)] = (owner, late.get(id(owner), (owner, 0))[1] + 1)
        for execution, count in late.values():
            if not (execution.passed and execution.expect is not None):
                continue
            errors, _ = _record_checks(execution.expect, execution.llms, execution.retrievals)
            if errors:
                execution.passed = False
                self._findings.append(
                    _Finding(
                        execution,
                        Status.FAILED,
                        f"{count} record(s) arrived after the test's window closed "
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

    def _run_check(
        self,
        target: str,
        result: TestResult,
        execution: _Execution,
        log: _TurnLog,
        values: Values,
        response: str,
    ) -> TestResult:
        """``expect.check`` (HB-12): the author's function, on what highbar knows of the turn.

        Its failures fail the turn. One that raises, or does not return a list of
        messages, leaves the turn unverified, unless it already failed (combine()).
        """
        if self._session is None:
            raise RuntimeError(f"expect.check {target}: its function was never loaded")
        why = log.unobserved
        tools = sessions.Unobserved("tools", why) if why else tuple(log.tools)
        turns = sessions.Unobserved("turns", why) if why else tuple(log.turns)
        ctx = sessions.CheckContext(
            test_id=values.test_id,
            test_name=values.test_name,
            spec_path=self._spec_path,
            message=log.trigger,
            response=response,
            session=dict(values.session),
            args=dict(values.args),
            history=tuple(values.history),
            tools=tools,
            turns=turns,
        )
        error: str | None = None
        try:
            failures = self._session.check(target, ctx)
        except Exception as e:
            failures, error = [], str(e) if isinstance(e, SessionError) else f"expect.check: {e!r}"
        # Read and caught, the records of a turn nobody saw still leave it unverified.
        refused = [r for r in (tools, turns) if isinstance(r, sessions.Unobserved)]
        error = next((r.message for r in refused if r.read), error)
        if error is not None:
            execution.passed = False
            status = Status.FAILED if result.status is Status.FAILED else Status.ERROR
            return replace(result, status=status, errors=[*result.errors, error])
        if not failures:
            return result
        execution.passed = False
        return replace(result, status=Status.FAILED, errors=[*result.errors, *failures])

    def _check(
        self, tools: list[Trace], response: str, expect: Expect, *, assume_unrecorded: bool = False
    ) -> list[str]:
        assertions = self._assertions
        if assume_unrecorded:
            assertions = [
                FunctionCallsAssertion(assume_unrecorded=True)
                if isinstance(a, FunctionCallsAssertion)
                else a
                for a in assertions
            ]
        failed = [
            r
            for assertion in assertions
            for r in assertion.evaluate(tools, response, expect)
            if not r.passed
        ]
        for r in failed:
            if r.condition is not None:
                self._conditions[r.message] = r.condition
        return [r.message for r in failed]

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
        if self._ceiling is not None:
            self._stop_after_flight(self._ceiling)
            return
        over = over_limit(self._costs(), self._max_cost)
        if over:
            self._stop_reason = BUDGET_EXCEEDED
            self._stopped = (
                f"--max-cost {amount(self._max_cost)}: the run spent {_amounts(over)}"
                f"{self._untagged_share(over)} by the end of '{last_test}', so it stopped "
                "there; raise the ceiling to run the rest"
            )

    def _stop_after_flight(self, ceiling: _Ceiling) -> None:
        """--max-cost with --jobs, once the tests in flight ended: what they spent past the
        ceiling is said, never passed in silence (D4)."""
        assert self._max_cost is not None
        self._stop_reason = BUDGET_EXCEEDED
        head = (
            f"--max-cost {amount(self._max_cost)}: the run spent {ceiling.spent} by the end "
            f"of '{ceiling.test}', so it"
        )
        if not ceiling.in_flight:
            self._stopped = f"{head} stopped there; raise the ceiling to run the rest"
            return
        over = over_limit(self._costs(), self._max_cost)
        past = {
            name: add_costs({name: cost}, {name: -self._max_cost})[name]
            for name, cost in over.items()
        }
        tests = "1 test" if ceiling.in_flight == 1 else f"{ceiling.in_flight} tests"
        self._stopped = (
            f"{head} started no more tests; the {tests} in flight then finished, and the run "
            f"spent {_amounts(over)} in all, {_amounts(past)} over the ceiling; raise the "
            "ceiling to run the rest"
        )

    def _tally(self, execution: _Execution, result: TestResult, name: str) -> None:
        """After each execution: --max-errors of them in a row that failed for one cause
        of the infrastructure stop the run there, in the middle of a test if need be.

        Only an error whose transport or HTTP failed counts. Any other ending of an
        execution (passed, failed, not observed, a session or template error, a reply
        that cannot be read) starts the count over: an agent that fails now and then is
        what min_pass is for (HB-34).
        """
        if not self._max_errors:
            return
        cause = execution.cause if result.status is Status.ERROR else None
        count = self._streak.add(cause)
        # With --jobs, a test in flight can end one more after the stop: the first says why.
        if cause is not None and count >= self._max_errors and self._stopped is None:
            self._stop_reason = REPEATED_ERRORS
            self._stopped = stop_message(count, cause, name)

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
        where = self._collector.path.absolute()
        if file_collector.is_glob(where):
            return (
                f"no file matches agent.traces {where}, so no test could see what the agent "
                "does; nothing was sent. Check agent.traces (a relative path starts where "
                "highbar runs): with {pid} in HIGHBAR_TRACES_URL, each process of the agent "
                "writes its own file, from when the SDK starts in it"
            )
        return (
            f"the trace file {where} does not exist, so no test "
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
                    if (ended := self._agent_exit()) is not None:
                        raise AgentNotObservedError(
                            f"{self._exit_text(ended)} right after its SDK's hello"
                            + (
                                ", and another process answers on its port"
                                if ended.port_answers
                                else ""
                            )
                            + f": tests would not talk to it. See {self._adapter.log_path}"
                        )
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
        self._args = test.session_args
        self._history = test.history
        runs, min_pass = resolve_runs(test, self._default_runs, self._default_min_pass)
        budget = self._budget_of(test)

        if test.turns:
            turns = list(test.turns)
        else:
            if test.trigger is None or test.expect is None:
                raise ValueError("Single-turn test must have both 'trigger' and 'expect'")
            # The test's budget is its execution's, checked once, on the sum of its turns.
            turns = [Turn(trigger=test.trigger, expect=replace(test.expect, budget=None))]
        # A paraphrase replaces the last turn's trigger, a single-turn test's only one; the
        # turns before it are sent as written (HB-16).
        last = turns[-1]
        sentences = [last.trigger]
        if test.variations:
            earlier = [turn.trigger for turn in turns[:-1]]
            paraphrases = self._paraphrases(last.trigger, test.variations, earlier)
            if isinstance(paraphrases, str):
                return TestResult(
                    status=Status.ERROR,
                    errors=[paraphrases],
                    duration_ms=(time.time() - start) * 1000,
                )
            sentences.extend(paraphrases)
        first: tuple[str, str | None] = (
            ("conversation", None) if test.turns else ("trigger", last.trigger)
        )
        kinds = [first, *(("variation", sentence) for sentence in sentences[1:])]
        phrasings = [
            # The failing phrasing is the whole finding: without it the report says
            # the intent broke but not which sentence broke it. Index 0 is the
            # sentence the spec author wrote, not a generated one.
            (
                f'[{"trigger" if index == 0 else "variation"}: "{sentence}"] '
                if test.variations
                else "",
                [*turns[:-1], replace(last, trigger=sentence)],
            )
            for index, sentence in enumerate(sentences)
        ]

        errors: list[str] = []
        traces: list[Trace] = []
        gate = _Gate(min_pass, counted=runs > 1 and not test.variations, each=runs)
        self._gates[self._test_index] = gate
        # Every phrasing runs even after one fails: three of ten broken is a different
        # bug from one of ten, and stopping early hides which. They all go to the same
        # running agent, back to back, with no reset in between. Only --max-errors stops
        # them, checked after each execution: the infrastructure can go down at any one.
        for index, (label, turns) in enumerate(phrasings):
            ran: list[Status] = []
            gate.runs.append(ran)
            gate.phrasings.append(kinds[index])
            results: list[TestResult] = []
            for run in range(runs):
                # Each run is its own execution, with its own test id and session.
                result = self._execute(
                    turns,
                    time.time(),
                    labelled=bool(test.turns),
                    name=name,
                    label=label,
                    phrasing=index,
                    run=run,
                    budget=budget,
                )
                results.append(result)
                ran.append(result.status)
                execution = self._local.execution
                assert execution is not None
                self._tally(execution, result, name)
                if self._stopped is not None or self._interrupting:
                    break
            passed = sum(1 for r in results if r.passed)
            errors.extend(label + error for error in _merged_errors(results, self._conditions))
            if runs > 1 and test.variations and passed < min_pass:
                errors.append(f"{label}{passed}/{len(results)} runs passed (min {min_pass})")
            for r in results:
                traces.extend(r.traces)
            if self._stopped is not None or self._interrupting:
                break

        done, planned = sum(len(statuses) for statuses in gate.runs), runs * len(phrasings)
        if done < planned:
            # The executions that ran stay as they ran; the test is not a verdict (HB-34).
            gate.interrupted = True
            errors.append(
                f"the run stopped before this test ended: {done} of {planned} executions ran"
            )

        return TestResult(
            status=gate.status,
            errors=errors,
            duration_ms=(time.time() - start) * 1000,
            traces=traces,
            counts=gate.counts,
        )

    def _paraphrases(self, trigger: str, count: int, earlier: list[str]) -> list[str] | str:
        """The generated paraphrases, or why there are none.

        ``earlier`` is the conversation before the turn paraphrased, its user messages:
        "and the deadline?" means nothing alone.
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
            if earlier:
                paraphrases = self._variations.generate(trigger, count, context=earlier)
            else:
                # A single-turn test asks as before: a generator of its own may not take
                # a context.
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
            test_id=uuid.uuid4().hex,
            test_name=name,
            spec_path=self._spec_path,
            args=self._args,
            history=self._history,
            run=self._run_values,
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
        self._local.execution = execution
        session: dict[str, Any] = {}
        if self._session is not None:
            try:
                with self._unlocked():
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
                    with self._unlocked():
                        self._session.teardown(context, session)
                except Exception as e:
                    # The test's result stands; a session left open is worth knowing.
                    reason = str(e) if isinstance(e, SessionError) else f"{type(e).__name__}: {e}"
                    self._warn(f"{name}: session teardown failed: {reason}")
            self._release(execution)

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
            message=None,
            test_id=context.test_id,
            test_name=context.test_name,
            session=session,
            args=context.args,
            history=context.history,
            run=context.run,
        )
        all_traces: list[Trace] = []
        all_errors: list[str] = []
        statuses: list[Status] = []

        for number, turn in enumerate(turns, start=1):
            # Which turn broke is the finding: "Expected X" alone does not say where.
            replies = [log.response for log in execution.turns]
            prefix = (
                f"turn {number}/{len(turns)} ({_turn_label(turn, all_traces, replies, session)}): "
                if labelled
                else ""
            )
            result = self._run_turn(turn.trigger, turn.expect, start, all_traces, values, execution)
            all_traces.extend(result.traces)
            all_errors.extend(prefix + error for error in result.errors)
            for error in result.errors:
                if prefix and error in self._conditions:
                    self._conditions[prefix + error] = prefix + self._conditions[error]
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
        earlier = [turn.response for turn in execution.turns]  # this execution's, so far
        log = _TurnLog(trigger=trigger, budget=expect.budget)
        execution.turns.append(log)
        # A budget on the whole execution reads the usage of each turn, whatever it expects.
        observes = expect.requires_observation or execution.budget is not None

        def unverified(error: str) -> TestResult:
            duration = (time.time() - start) * 1000
            return TestResult(status=Status.ERROR, errors=[error], duration_ms=duration)

        try:
            resolved_trigger = resolve_template(trigger, previous_traces, earlier, values.session)
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
        if self._parallel and execution.held is None:
            # In flight from here: what other reads take in for it is kept for it.
            execution.held = _Held()
        try:
            with self._unlocked():
                response = self._client.send(
                    resolved_trigger, values=replace(values, message=resolved_trigger)
                )
        except AgentResponseError as e:
            # A failure of the transport or of HTTP is what --max-errors counts (HB-34).
            execution.cause = e.cause
            return unverified(str(e))
        except PlaceholderError as e:
            return unverified(f"agent.request: {e}")
        except Exception as e:
            # The message never reached the agent, so its behavior was not checked.
            return unverified(f"HTTP error: {e}")
        log.response, log.response_ms = response, (time.time() - sent_at) * 1000

        with self._unlocked():
            time.sleep(self._settle_time)
        window, unreadable = self._read()
        self._cursor = window.end
        execution.read = True
        self._absorb(window)
        shared = Window()
        if self._parallel:
            window, shared, unreadable = self._distribute(window, execution, unreadable)
        previous = self._last_read(execution)
        result = self._verdict(
            window,
            response,
            expect,
            execution,
            log,
            previous,
            start,
            observes=observes,
            shared=shared,
        )
        if expect.check is not None and result.status is not Status.ERROR:
            result = self._run_check(expect.check, result, execution, log, values, response)
        gone = self._look_at_agent(current=execution)
        if gone is not None and execution.on_faith:
            execution.passed = False
            result = replace(result, status=Status.ERROR, errors=[gone])
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
        shared: Window | None = None,
    ) -> TestResult:
        """What ``window`` says of this turn: its records counted, and it checked against them.

        ``shared`` (--jobs) holds the untagged records other reads took in while this turn
        was in flight: they may be its own, as the untagged ones in ``window`` may be.
        """

        def unverified(error: str) -> TestResult:
            duration = (time.time() - start) * 1000
            return TestResult(status=Status.ERROR, errors=[error], duration_ms=duration)

        classified = classify(window, execution.correlate)
        # What other reads shared with this one: only what has none of its keys either.
        shared = classify(shared or Window(), execution.correlate).unattributed
        untagged = classified.unattributed.tools
        if (untagged or shared.tools) and (classified.correlated or self._parallel):
            self._warn_once(UNATTRIBUTED_WARNING, execution.correlate)
        elif untagged and (self._managed or not observes):
            # An attached test that checks tool calls errors instead, and says why (FR2).
            self._warn_once(NO_CONTEXT_WARNING, execution.correlate)

        # With --jobs, nothing counts on faith: several tests are in flight (HB-8).
        if not classified.correlated and not self._parallel:
            stray = self._read_usage(window.turns)
            self._suspect_window(previous, stray, execution.correlate)
            if observes and not self._managed:
                # Attach mode: the agent may serve other traffic, and there is no
                # `hello` from a fresh file to rely on.
                return unverified(self._not_observed_attached(window, execution.correlate))
            # Nothing in the window is tagged: it is all there is to go by. An attached
            # agent may be serving others, and their tokens are not this test's.
            traces = window.tools
            llms, retrievals = window.llms, window.retrievals
            log.tools = list(traces)
            log.records = stray if self._managed else []
            log.turns = list(window.turns)
            execution.on_faith = execution.on_faith or self._managed
            if expect.check is not None and not (self._managed and observes):
                # highbar's own checks read an untagged window only in that case (a fresh
                # file whose hello it waited for): expect.check reads what they read.
                log.unobserved = self._not_observed_attached(window, execution.correlate)
            if log.records:
                self._warn_once(NO_CONTEXT_USAGE_WARNING, execution.correlate)
            errors = self._check(traces, response, expect)
        else:
            self._judge_late(classified.other.tools, current=execution)
            self._judge_late_usage(classified.other.turns, current=execution)
            self._judge_late_records(
                classified.other.llms, classified.other.retrievals, current=execution
            )
            self._suspect(previous, untagged)
            log.untagged = self._read_usage(classified.unattributed.turns)
            self._suspect_usage(previous, log.untagged, execution.correlate)
            # Shared, it is already in the run's spend once, from the read that took it in.
            log.untagged += self._read_usage(shared.turns)
            untagged = [*untagged, *shared.tools]
            mine = classified.mine
            seen = mine.tools or mine.turns or mine.llms or mine.retrievals
            if observes and not seen:
                if self._parallel and (
                    _any_record(classified.unattributed, shared) or not classified.correlated
                ):
                    return unverified(JOBS_NOT_OBSERVED.format(hint=_tag_hint(execution.correlate)))
                return unverified(self._not_observed(classified, execution.correlate))
            traces = classified.mine.tools
            llms, retrievals = classified.mine.llms, classified.mine.retrievals
            log.tools = list(traces)
            log.records = self._read_usage(classified.mine.turns)
            log.turns = list(classified.mine.turns)
            if self._managed and not seen:
                execution.on_faith = True
            if expect.check is not None and not seen:
                log.unobserved = self._not_observed(classified, execution.correlate)
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

        # A call whose return was not recorded: `returns` could not be checked on it.
        # A test that passes once those returns are taken to fit was not checked,
        # which is not the same as failed (review of #74).
        unseen_returns = unrecorded_returns(traces, expect) if errors else []
        if unseen_returns and not self._check(traces, response, expect, assume_unrecorded=True):
            return unverified(
                f"the return of {_quoted(unseen_returns)} was not recorded, so "
                "`returns` was not checked: give hb.record(...) the result=, or "
                "write a `result` in the tool's line"
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

        failures, unseen = _record_checks(expect, llms, retrievals)
        errors = [*errors, *failures]
        if unseen:
            # No record of a type it checks: nothing was checked there, which only a
            # failure outranks (decision E2).
            if not errors:
                return unverified("; ".join(unseen))
            errors = [*errors, *unseen]

        execution.expect, execution.response = expect, response
        execution.llms, execution.retrievals = list(llms), list(retrievals)
        execution.tools, execution.passed = list(traces), not errors
        duration = (time.time() - start) * 1000
        return TestResult(
            status=Status.FAILED if errors else Status.PASSED,
            errors=errors,
            duration_ms=duration,
            traces=traces,
        )
