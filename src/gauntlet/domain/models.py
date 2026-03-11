"""Core domain models for Gauntlet specs, traces, and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    """Expected function call assertion."""

    name: str
    params: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


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


@dataclass(frozen=True)
class Test:
    """A single test: either a trigger+expect or a list of turns."""

    trigger: str | None = None
    expect: Expect | None = None
    turns: list[Turn] | None = None


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

    passed: bool
    errors: list[str]
    duration_ms: float
    traces: list[Trace] = field(default_factory=list)


@dataclass(frozen=True)
class RunSummary:
    """Summary of a complete test run."""

    total: int
    passed: int
    failed: int
    duration_ms: float
