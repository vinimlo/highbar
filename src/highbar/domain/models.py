"""Core domain models for Highbar specs, traces, and results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import MISSING, Field, dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast


class Status(StrEnum):
    """How a test ended.

    ``ERROR`` means the behavior could not be checked (the agent was unreachable, the
    paraphrases were missing): nothing was verified, so it is neither a pass nor a
    failure of the agent, and it must never be reported as either.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


def combine(statuses: Iterable[Status]) -> Status:
    """The status of a test made of parts (turns, phrasings).

    Any failure wins: the agent did something wrong, and that holds even if another
    part could not be checked. Otherwise any error makes the whole unverified.
    """
    seen = set(statuses)
    if Status.FAILED in seen:
        return Status.FAILED
    if Status.ERROR in seen:
        return Status.ERROR
    return Status.PASSED


# What agent.traces.format names: highbar's own contract, or OTLP JSON lines (HB-48).
TRACE_FORMATS = ("highbar", "otlp")


@dataclass(frozen=True)
class Trace:
    """One tool call, from a `tool` record of the trace contract.

    ``error`` is the only signal that the call failed: ``result`` is never inspected,
    because a tool may legitimately return an object with an ``error`` key.
    ``context`` holds the record's correlation fields (``test``, ``conversation_id``...).
    """

    fn: str
    args: dict[str, Any]
    result: Any
    started_at: float
    ended_at: float
    error: str | None = None
    context: dict[str, Any] = field(default_factory=dict[str, Any])
    # False when the record had no `result` key: what the tool returned was not seen,
    # which is not the same as having returned null (review of #74, HB-37).
    has_result: bool = True
    # False when the arguments were not recorded: an OpenTelemetry tool span exports
    # them only with content capture on (HB-48). `params` cannot be checked on it.
    has_args: bool = True

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000


@dataclass(frozen=True)
class Usage:
    """What handling a message consumed, from the optional `usage` of a `turn` record.

    Every field is optional, and None means the emitter did not say, which is not zero.
    ``input_tokens`` is the whole input, with ``cached_input_tokens`` already in it.

    A message with several records has their sum here, unknown wherever one of them says
    nothing. ``records`` keeps each one, so that what is known still counts: sums and
    budgets read those. ``unattributed`` is usage read next to them that no context ties
    to a test: it counts for none, and may still be this message's. Neither is part of
    the value, or of the report.
    """

    model: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost: float | None = None
    currency: str | None = None
    llm_calls: int | None = None
    records: tuple[Usage, ...] = field(default=(), compare=False)
    unattributed: tuple[Usage, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class TurnRecord:
    """A `turn` record: the instrumented agent received and handled a message."""

    ts: float
    context: dict[str, Any] = field(default_factory=dict[str, Any])
    message: str | None = None
    response: str | None = None
    usage: Usage | None = None
    usage_unreadable: bool = False  # it had a `usage`, and nothing in it could be read


@dataclass(frozen=True)
class LlmCall:
    """An `llm` record: one call to the model, a failed one or a fallback included (HB-11).

    Its tokens are shown, not summed: the `turn`'s usage is what budgets read, so
    nothing counts twice (decision E3, on the card).

    ``source`` says who wrote it: ``"proxy"`` for highbar's LLM proxy (HB-30), None for
    the agent. ``read`` is False when the writer saw the call and could not read its
    usage, with ``unread`` saying why; None means the writer did not say. A token field
    that is None is unknown, never zero, and an unread call's count is not whole.
    """

    ts: float
    context: dict[str, Any] = field(default_factory=dict[str, Any])
    model: str | None = None
    provider: str | None = None
    attempt: int | None = None
    fallback_from: str | None = None
    roles: tuple[str, ...] | None = None  # the roles of the messages sent, in order
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None
    latency_ms: float | None = None
    error: str | None = None
    source: str | None = None
    read: bool | None = None
    unread: str | None = None  # why the usage was not read, when read is False
    cost: float | None = None  # what the provider said the call cost (OpenRouter)
    currency: str | None = None


@dataclass(frozen=True)
class RetrievedItem:
    """One document in a prompt: a training, a manual. ``source``: retrieved, expanded or
    sticky (kept from an earlier turn). ``chunks``: the indexes of its parts that went in."""

    id: str | None = None
    title: str | None = None
    source: str | None = None
    chunks: tuple[int, ...] = ()
    tokens: int | None = None


@dataclass(frozen=True)
class Retrieval:
    """A `retrieval` record: what entered the prompt for one message (HB-3)."""

    ts: float
    context: dict[str, Any] = field(default_factory=dict[str, Any])
    query: str | None = None
    search_query: str | None = None
    strategy: str | None = None
    skipped_reason: str | None = None
    items: tuple[RetrievedItem, ...] = ()
    expired: tuple[dict[str, Any], ...] = ()
    tokens: int | None = None


@dataclass(frozen=True)
class Hello:
    """A `hello` record: an emitter started writing to the trace file."""

    ts: float
    sdk: str | None = None
    pid: int | None = None


@dataclass(frozen=True)
class Window:
    """The records written to the trace file between two points of a run."""

    tools: list[Trace] = field(default_factory=list[Trace])
    turns: list[TurnRecord] = field(default_factory=list[TurnRecord])
    hellos: list[Hello] = field(default_factory=list[Hello])
    llms: list[LlmCall] = field(default_factory=list[LlmCall])
    retrievals: list[Retrieval] = field(default_factory=list[Retrieval])
    # The tools the agent declared it records, one tuple per declaration (spec D4).
    declarations: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    # Where the next read starts: every record is read once, none falls between reads.
    end: int = 0


@dataclass(frozen=True)
class RequestConfig:
    """How a trigger is sent. Values may hold {{message}}, {{test.*}} and {{session.*}}."""

    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict[str, str])
    body: Any = field(default_factory=lambda: {"message": "{{message}}"})


@dataclass(frozen=True)
class CommandHook:
    """A session hook run as a command; placeholders are resolved per argument."""

    argv: list[str]


@dataclass(frozen=True)
class PythonHook:
    """A session hook loaded once in highbar's process: ``"module:function"``."""

    target: str


@dataclass(frozen=True)
class HttpHook:
    """A session hook sent as an HTTP request; placeholders are resolved in each part.

    ``body`` is sent as JSON, and ``None`` sends no body. A setup's reply must be a JSON
    object, which becomes the session.
    """

    url: str
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict[str, str])
    body: Any = None


Hook = CommandHook | PythonHook | HttpHook


@dataclass(frozen=True)
class SessionConfig:
    """Clean state per test execution: ``setup`` opens a session, ``teardown`` closes it."""

    setup: Hook | None = None
    teardown: Hook | None = None


@dataclass(frozen=True)
class RunConfig:
    """Once per spec (HB-10): ``setup`` before the agent starts, ``teardown`` on every way
    out of a run whose setup finished, Ctrl-C and SIGTERM included."""

    setup: Hook | None = None
    teardown: Hook | None = None
    # The spec reads what the setup returns as {{run.*}} (v1): only then must the setup
    # return a JSON object. A setup whose result nobody reads may print what it likes.
    read_result: bool = False


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for the agent under test.

    With ``start`` highbar starts and stops the agent (managed mode); without it the
    agent is already running (attach mode), and ``traces`` names the file it writes.
    """

    endpoint: str
    start: str | None = None
    health: str | None = None
    startup_timeout: int = 15000
    settle_time: int = 500
    timeout: int = 60000
    request: RequestConfig = field(default_factory=RequestConfig)
    response: str | None = None
    # Where the reply text comes from: the HTTP body, or the `response` of the test
    # execution's turn record, for an agent that answers later through a queue (HB-14).
    reply: str = "body"
    traces: str | None = None
    # How the trace file is written: highbar's own contract, or OTLP JSON lines (HB-48).
    trace_format: str = "highbar"
    session: SessionConfig | None = None
    run: RunConfig | None = None
    correlate: dict[str, Any] = field(default_factory=lambda: {"test": "{{test.id}}"})
    # Each value expanded from ${NAME}, and the name: what output must never show.
    secrets: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def managed(self) -> bool:
        return self.start is not None


@dataclass(frozen=True)
class FunctionCallExpect:
    """Expected function call assertion.

    ``name`` may be a list, in which case ANY of the listed tools satisfies the
    expectation. Some intents have more than one correct answer — "show me the menu"
    can be served by opening a catalog widget or by listing categories as text, and
    both serve the user. Without alternatives the only options are to pick one and
    accuse the agent of failing when it succeeded another way, or to not test the
    intent at all.
    """

    name: str | list[str]
    params: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    # The whole return value, as an expected value (a matcher, a list, a text): for a
    # tool that returns a list or a text, which `result`'s key names cannot reach
    # (HB-37). None: not checked; a null return is ``{equals: null}``.
    returns: Any = None
    # False: the call must not have failed. True: it must have. A matcher: it must
    # have failed with an error that matches (``{contains: "Timeout"}``).
    error: bool | dict[str, Any] = False

    @property
    def accepted_names(self) -> list[str]:
        """The tool names that satisfy this expectation."""
        if isinstance(self.name, list):
            return self.name
        return [self.name]


@dataclass(frozen=True)
class ResponseExpect:
    """Expected response text: a matcher, with the vocabulary of ``params`` (D8).

    ``{contains: "shipped", not_matches: "\\b4[0-9]{3}..."}``: every condition must hold.
    """

    matcher: dict[str, Any]


@dataclass(frozen=True)
class Budget:
    """What a turn, or a whole execution, may spend. A value equal to a limit is within it.

    ``max_input_tokens`` counts the whole input, cached tokens included. ``max_cost`` has
    no currency of its own: it holds for the sum in each currency the costs came in.
    """

    max_input_tokens: int | None = None
    max_cost: int | float | None = None


@dataclass(frozen=True)
class LlmExpect:
    """``expect.llm`` (HB-11): checks of the test's calls to the model (decision E4).

    ``model`` is a text matcher on the call that answered, the last without ``error``.
    ``last_role`` holds for every call. ``fallback`` is whether any call came from another
    model or was a retry. ``max_calls`` counts the failed calls too.
    """

    model: dict[str, Any] | None = None
    fallback: bool | None = None
    last_role: str | None = None
    max_calls: int | None = None


@dataclass(frozen=True)
class ItemRef:
    """A document named in ``expect.retrieval.includes``: its id or title, and how it
    must have come in, when that matters."""

    key: str
    source: str | None = None


@dataclass(frozen=True)
class RetrievalExpect:
    """``expect.retrieval`` (HB-3). ``includes``/``excludes`` read every record of the test;
    ``max_items``/``max_tokens`` hold for each record, one prompt each."""

    includes: tuple[ItemRef, ...] = ()
    excludes: tuple[str, ...] = ()
    max_items: int | None = None
    max_tokens: int | None = None
    chunks_in_order: bool = False


@dataclass(frozen=True)
class Expect:
    """Expected outcomes for a test or turn."""

    function_calls: list[FunctionCallExpect] | None = None
    response: ResponseExpect | None = None
    ordered: bool = False  # the expected calls happen in this order
    exclusive: bool = False  # no call beyond the expected ones
    forbid: tuple[str, ...] = ()  # tools that must not be called, failed or not
    budget: Budget | None = None  # per turn in a turn, per execution in a test
    check: str | None = None  # "module:function" of the author's own check (HB-12)
    llm: LlmExpect | None = None  # the calls to the model (HB-11)
    retrieval: RetrievalExpect | None = None  # what entered the prompt (HB-3)

    @property
    def requires_observation(self) -> bool:
        """Checks what the agent recorded, so it needs proof that it saw the message.

        ``function_calls: []`` and ``forbid`` included: "no tool was called" means
        nothing unless the message reached an agent that would have recorded one. A
        budget reads the usage of the turn's own record.
        """
        return (
            self.function_calls is not None
            or bool(self.forbid)
            or self.budget is not None
            or self.llm is not None
            or self.retrieval is not None
        )


@dataclass(frozen=True)
class Turn:
    """A single turn in a multi-turn test."""

    trigger: str
    expect: Expect
    name: str | None = None


@dataclass(frozen=True)
class Test:
    """A single test: either a trigger+expect or a list of turns.

    ``variations`` asks for N paraphrases of ``trigger`` to be generated and run through
    the same assertions. The agent must route the intent correctly however the user
    words it — not just for the one phrasing the product team happened to write.

    With ``turns``, ``expect`` holds only a ``budget``, for the whole conversation: what
    the conversation must do is in the ``expect`` of each turn.
    """

    trigger: str | None = None
    expect: Expect | None = None
    turns: list[Turn] | None = None
    variations: int = 0
    name: str | None = None
    # How many times each phrasing runs, and how many of those must pass (D9). None
    # takes the run's default; min_pass may be a fraction of runs.
    runs: int | None = None
    min_pass: int | float | None = None
    # What joins this test across reports; without it, the key is suite/name (D15).
    id: str | None = None
    # Its own ``session: {args: ...}`` (HB-9): the state its hooks open, and
    # ``{{test.args.*}}`` in the request. A JSON object, checked when the spec loads.
    session_args: dict[str, Any] = field(default_factory=dict[str, Any])
    # The conversation before its first message (HB-24): {role, content} each, for the
    # session's setup to seed. highbar sends none of it.
    history: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    # The state a negative control must end in (HB-21): ``failed`` or ``error``. The test
    # passes when it ends exactly there; None: it must pass, as every test.
    expect_status: Status | None = None

    @property
    def requires_observation(self) -> bool:
        own = self.expect is not None and self.expect.requires_observation
        return own or any(turn.expect.requires_observation for turn in self.turns or [])

    @property
    def plain_name(self) -> str:
        """The test's name for {{test.name}}, hooks and keys: no counts, no decoration."""
        return self.name or self.trigger or f"multi-turn ({len(self.turns or [])} turns)"

    @property
    def definition_sha256(self) -> str:
        """A hash of what the test measures, to tell which results can be put together.

        Labels (``name``, ``id``) and sampling (``runs``, ``min_pass``) stay out: they
        change neither the message nor what counts as right (D16).
        """
        definition: dict[str, Any] = {
            "trigger": self.trigger,
            "expect": self.expect,
            "variations": self.variations,
            "turns": [
                {"trigger": turn.trigger, "expect": turn.expect} for turn in self.turns or []
            ],
        }
        if self.session_args:
            # The state a test starts from is part of what it measures. Only when there
            # is one: the hash of every test without it stays what it was.
            definition["session_args"] = self.session_args
        if self.history:
            definition["history"] = self.history
        if self.expect_status is not None:
            # It changes what counts as right. Only when set, as session_args and history.
            definition["expect_status"] = str(self.expect_status)
        # ASCII, so a lone surrogate from YAML is an escape and not a failed encode.
        text = json.dumps(_canonical(definition), sort_keys=True, default=str)
        return hashlib.sha256(text.encode("ascii")).hexdigest()


def _default(declared: Field[Any]) -> Any:
    if declared.default_factory is not MISSING:
        return declared.default_factory()
    return declared.default


def _canonical(value: Any) -> Any:
    """``value`` the same way however it was written, for a hash.

    Keys are text: YAML's can be booleans, dates or null, and JSON sorts text. A field
    of a dataclass at its default stays out, so a new optional field changes the hash
    of no test that does not use it.
    """
    if is_dataclass(value) and not isinstance(value, type):
        values = {declared: getattr(value, declared.name) for declared in fields(value)}
        return {
            declared.name: _canonical(item)
            for declared, item in values.items()
            if item != _default(declared)
        }
    if isinstance(value, dict):
        mapping = cast("dict[Any, Any]", value)
        return {str(key): _canonical(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast("list[Any] | tuple[Any, ...]", value)
        return [_canonical(item) for item in items]
    return value


def stable_key(suite: str, test: Test) -> str:
    """The name of a test across runs: its ``id``, or ``<suite>/<name>`` (D15)."""
    return test.id or f"{suite}/{test.plain_name}"


@dataclass(frozen=True)
class Suite:
    """A named group of tests."""

    name: str
    tests: list[Test]


@dataclass(frozen=True)
class Spec:
    """A complete spec file: agent config + suites."""

    agent: AgentConfig
    suites: list[Suite]
    default_runs: int = 1  # defaults.runs; the CLI's --runs overrides it
    # defaults.min_pass, for the tests without their own; None: every run must pass.
    default_min_pass: int | float | None = None
    # defaults.budget: what an execution may spend, for the tests without their own.
    default_budget: Budget | None = None


@dataclass(frozen=True)
class AssertionResult:
    """Result of a single assertion evaluation."""

    passed: bool
    message: str
    # What a failure says apart from the agent's reply, which changes on every run: a
    # phrasing's runs are grouped by it (HB-27). None when the message is all of it.
    condition: str | None = None


@dataclass(frozen=True)
class TestResult:
    """Result of running a single test."""

    status: Status
    errors: list[str]
    duration_ms: float
    traces: list[Trace] = field(default_factory=list[Trace])
    # (passed, runs, min_pass) when a test without variations ran more than once.
    counts: tuple[int, int, int] | None = None
    # The test's expect_status (HB-21): ``status`` is already judged against it.
    expected: Status | None = None

    @property
    def passed(self) -> bool:
        return self.status is Status.PASSED


def iso_utc(timestamp: float) -> str:
    """``2026-10-30T11:49:00.120Z``: the form every time in a result takes."""
    moment = datetime.fromtimestamp(timestamp, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Counts:
    """How the executions of a test ended, in the three states of ``Status``.

    There is no rate: with errors among them, the denominator is a choice, and whoever
    computes one has to see that they are there (D14).
    """

    passed: int = 0
    failed: int = 0
    error: int = 0


@dataclass(frozen=True)
class ToolOutcome:
    """A call the agent made. ``result`` and ``context`` stay in the trace file (D18)."""

    name: str
    args: dict[str, Any]
    error: str | None
    duration_ms: float
    late: bool = False  # recorded after the test's window closed


@dataclass(frozen=True)
class UsageTotals:
    """The tokens and cost of a level of the result, added up (``engine.usage.add_up``).

    Only what is known goes in, and ``warnings`` says what was left out: with any,
    ``complete`` is False. A count is None when no turn reported it, which is not zero.
    ``cost`` has one sum per currency, because two currencies do not add up to a number.
    """

    turns: int  # how many turns are behind these numbers, with usage or without
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    llm_calls: int | None = None
    cost: dict[str, float] = field(default_factory=dict[str, float])
    complete: bool = True
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnOutcome:
    """One message of an execution.

    ``response`` is None when no reply came, which is not the same as an empty one.
    ``response_ms`` is the send alone, without ``settle_time``. ``correlate`` holds the
    values that lead to this turn's records in the trace file (D18). ``usage`` is None
    when the turn reported none, and ``budget`` is the turn's own limit, if it has one.
    """

    trigger: str
    response: str | None
    response_ms: float | None
    correlate: dict[str, Any]
    tools: tuple[ToolOutcome, ...] = ()
    usage: Usage | None = None
    budget: Budget | None = None


@dataclass(frozen=True)
class ExecutionOutcome:
    """One run of one phrasing: one test id and one session."""

    execution_id: str
    run: int  # from 1
    started_at: str
    status: Status
    duration_ms: float
    errors: tuple[str, ...] = ()
    turns: tuple[TurnOutcome, ...] = ()
    totals: UsageTotals | None = None  # None: no turn reported usage


@dataclass(frozen=True)
class PhrasingOutcome:
    """The trigger, one of its variations, or the conversation of a multi-turn test."""

    kind: str  # "trigger", "variation" or "conversation"
    text: str | None  # None for a conversation: each turn has its own
    status: Status
    counts: Counts
    executions: tuple[ExecutionOutcome, ...] = ()


@dataclass(frozen=True)
class TestOutcome:
    """A test in its final state: late calls included, values from ${VAR} masked."""

    id: str  # the key that joins it across reports, see stable_key()
    suite: str
    name: str
    definition_sha256: str
    status: Status
    runs: int
    min_pass: int
    counts: Counts
    duration_ms: float
    errors: tuple[str, ...] = ()
    phrasings: tuple[PhrasingOutcome, ...] = ()
    budget: Budget | None = None  # what each execution may spend, default included
    totals: UsageTotals | None = None
    # The state the test expects to end in (HB-21); ``status`` is judged against it, and
    # its phrasings and executions keep what happened.
    expect_status: Status | None = None

    @property
    def usages(self) -> list[Usage | None]:
        """The usage of every turn that ran, None for a turn that reported none."""
        return [
            turn.usage
            for phrasing in self.phrasings
            for execution in phrasing.executions
            for turn in execution.turns
        ]


@dataclass(frozen=True)
class SuiteOutcome:
    """A suite's sums. Its tests carry the suite's name; a report keeps them in one list."""

    name: str
    totals: UsageTotals | None = None


# Why a run stopped before its end: RunSummary.stop_reason, and the report's stopped.reason.
BUDGET_EXCEEDED = "budget_exceeded"  # --max-cost
REPEATED_ERRORS = "repeated_errors"  # --max-errors


@dataclass(frozen=True)
class RunSummary:
    """Summary of a complete test run.

    ``variations`` counts the paraphrases that ran, each once however many ``runs`` it
    had. They are extra phrasings of a test, not extra tests: a test with 5 variations
    still passes or fails as one.

    ``tests`` holds every finished test in its final state, and the counts are theirs:
    a late call can change a test after its result was first shown.

    ``stopped`` says why the run ended before its last test, and ``stop_reason`` which
    stop it was: ``BUDGET_EXCEEDED`` (its cost ceiling, at the end of a test) or
    ``REPEATED_ERRORS`` (executions in a row that failed for one cause of the
    infrastructure, which can come in the middle of a test). ``tests`` then holds the
    ones that ran, and a test that stopped halfway is an error with the executions it had.

    ``untagged_cost`` is what an agent highbar started spent, per currency, in `turn`
    records with no test context: in no total, and counted by the cost ceiling.
    """

    total: int
    passed: int
    failed: int
    errored: int
    duration_ms: float
    variations: int = 0
    tests: tuple[TestOutcome, ...] = ()
    warnings: tuple[str, ...] = ()
    totals: UsageTotals | None = None
    suites: tuple[SuiteOutcome, ...] = ()
    stopped: str | None = None
    untagged_cost: dict[str, float] = field(default_factory=dict[str, float])
    stop_reason: str | None = None
