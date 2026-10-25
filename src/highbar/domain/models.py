"""Core domain models for Highbar specs, traces, and results."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000


@dataclass(frozen=True)
class TurnRecord:
    """A `turn` record: the instrumented agent received and handled a message."""

    ts: float
    context: dict[str, Any] = field(default_factory=dict)
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

    tools: list[Trace] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    hellos: list[Hello] = field(default_factory=list)
    # Where the next read starts: every record is read once, none falls between reads.
    end: int = 0


@dataclass(frozen=True)
class RequestConfig:
    """How a trigger is sent. Values may hold {{message}}, {{test.*}} and {{session.*}}."""

    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
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

    @property
    def accepted_names(self) -> list[str]:
        """The tool names that satisfy this expectation."""
        if isinstance(self.name, list):
            return self.name
        return [self.name]


@dataclass(frozen=True)
class ResponseExpect:
    """Expected response text assertion."""

    contains: str | None = None
    not_contains: str | None = None
    matches: str | None = None


@dataclass(frozen=True)
class Expect:
    """Expected outcomes for a test or turn."""

    function_calls: list[FunctionCallExpect] | None = None
    response: ResponseExpect | None = None

    @property
    def requires_observation(self) -> bool:
        """Checks tool calls, so it needs proof the instrumented agent saw the message.

        ``function_calls: []`` included: "no tool was called" means nothing unless the
        message reached an agent that would have recorded one.
        """
        return self.function_calls is not None


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

    @property
    def requires_observation(self) -> bool:
        if self.turns:
            return any(turn.expect.requires_observation for turn in self.turns)
        return self.expect is not None and self.expect.requires_observation


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
    traces: list[Trace] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status is Status.PASSED


@dataclass(frozen=True)
class RunSummary:
    """Summary of a complete test run.

    ``variations`` counts the generated paraphrases that were executed. They are extra
    *executions*, not extra tests: a test with 5 variations still passes or fails as one.
    """

    total: int
    passed: int
    failed: int
    errored: int
    duration_ms: float
    variations: int = 0
