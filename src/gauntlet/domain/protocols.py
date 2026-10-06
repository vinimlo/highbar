"""Protocol definitions for Gauntlet's extension points."""

from __future__ import annotations

from typing import Protocol

from gauntlet.domain.models import (
    AssertionResult,
    Expect,
    RunSummary,
    Suite,
    TestResult,
    Trace,
)


class Assertion(Protocol):
    """Evaluates captured traces and response against expected outcomes."""

    def evaluate(
        self, traces: list[Trace], response: str, expect: Expect
    ) -> list[AssertionResult]: ...


class Reporter(Protocol):
    """Formats and outputs test results."""

    def on_suite_start(self, suite: Suite) -> None: ...

    def on_test_result(self, test_name: str, result: TestResult) -> None: ...

    def on_run_end(self, summary: RunSummary) -> None: ...


class AgentAdapter(Protocol):
    """Manages the agent process lifecycle."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


class VariationGenerator(Protocol):
    """Produces N paraphrases of a trigger that keep the same intent."""

    def generate(self, trigger: str, count: int) -> list[str]: ...


class TraceCollector(Protocol):
    """Reads captured trace boxes."""

    def checkpoint(self) -> int: ...

    def collect(self, since: int) -> list[Trace]: ...
