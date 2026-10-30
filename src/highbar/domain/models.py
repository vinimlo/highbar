"""Core domain models for Highbar specs, traces, and results."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
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

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000


@dataclass(frozen=True)
class TurnRecord:
    """A `turn` record: the instrumented agent received and handled a message."""

    ts: float
    context: dict[str, Any] = field(default_factory=dict[str, Any])
    message: str | None = None
    response: str | None = None


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


Hook = CommandHook | PythonHook


@dataclass(frozen=True)
class SessionConfig:
    """Clean state per test execution: ``setup`` opens a session, ``teardown`` closes it."""

    setup: Hook | None = None
    teardown: Hook | None = None


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
    traces: str | None = None
    session: SessionConfig | None = None
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
class Expect:
    """Expected outcomes for a test or turn."""

    function_calls: list[FunctionCallExpect] | None = None
    response: ResponseExpect | None = None
    ordered: bool = False  # the expected calls happen in this order
    exclusive: bool = False  # no call beyond the expected ones
    forbid: tuple[str, ...] = ()  # tools that must not be called, failed or not

    @property
    def requires_observation(self) -> bool:
        """Checks tool calls, so it needs proof the instrumented agent saw the message.

        ``function_calls: []`` and ``forbid`` included: "no tool was called" means
        nothing unless the message reached an agent that would have recorded one.
        """
        return self.function_calls is not None or bool(self.forbid)


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

    @property
    def requires_observation(self) -> bool:
        if self.turns:
            return any(turn.expect.requires_observation for turn in self.turns)
        return self.expect is not None and self.expect.requires_observation

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
        definition = {
            "trigger": self.trigger,
            "expect": asdict(self.expect) if self.expect else None,
            "variations": self.variations,
            "turns": [
                {"trigger": turn.trigger, "expect": asdict(turn.expect)}
                for turn in self.turns or []
            ],
        }
        # ASCII, so a lone surrogate from YAML is an escape and not a failed encode.
        text = json.dumps(_canonical(definition), sort_keys=True, default=str)
        return hashlib.sha256(text.encode("ascii")).hexdigest()


def _canonical(value: Any) -> Any:
    """``value`` with text keys: YAML's can be booleans, dates or null, and JSON sorts text."""
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


@dataclass(frozen=True)
class AssertionResult:
    """Result of a single assertion evaluation."""

    passed: bool
    message: str


@dataclass(frozen=True)
class TestResult:
    """Result of running a single test."""

    status: Status
    errors: list[str]
    duration_ms: float
    traces: list[Trace] = field(default_factory=list[Trace])
    # (passed, runs, min_pass) when a test without variations ran more than once.
    counts: tuple[int, int, int] | None = None

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
class TurnOutcome:
    """One message of an execution.

    ``response`` is None when no reply came, which is not the same as an empty one.
    ``response_ms`` is the send alone, without ``settle_time``. ``correlate`` holds the
    values that lead to this turn's records in the trace file (D18).
    """

    trigger: str
    response: str | None
    response_ms: float | None
    correlate: dict[str, Any]
    tools: tuple[ToolOutcome, ...] = ()


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


@dataclass(frozen=True)
class RunSummary:
    """Summary of a complete test run.

    ``variations`` counts the paraphrases that ran, each once however many ``runs`` it
    had. They are extra phrasings of a test, not extra tests: a test with 5 variations
    still passes or fails as one.

    ``tests`` holds every finished test in its final state, and the counts are theirs:
    a late call can change a test after its result was first shown.
    """

    total: int
    passed: int
    failed: int
    errored: int
    duration_ms: float
    variations: int = 0
    tests: tuple[TestOutcome, ...] = ()
    warnings: tuple[str, ...] = ()
