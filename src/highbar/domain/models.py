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
    """An atomic box capturing a single tool invocation."""

    id: str
    seq: int
    fn: str
    args: dict[str, Any]
    result: Any
    started_at: float
    ended_at: float
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for the agent under test."""

    start: str
    endpoint: str
    health: str | None = None
    startup_timeout: int = 15000
    settle_time: int = 500


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
