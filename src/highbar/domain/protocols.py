"""Protocol definitions for Highbar's extension points."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from highbar.domain.models import (
    AssertionResult,
    Expect,
    RunSummary,
    Suite,
    TestResult,
    Trace,
    Window,
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

    def on_warning(self, message: str) -> None: ...


class AgentAdapter(Protocol):
    """Manages the agent process lifecycle."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


class VariationGenerator(Protocol):
    """Produces N paraphrases of a trigger that keep the same intent."""

    def generate(self, trigger: str, count: int) -> list[str]: ...


class TraceCollector(Protocol):
    """Reads the records an instrumented agent writes (the trace contract)."""

    @property
    def path(self) -> Path: ...

    def checkpoint(self) -> int: ...

    def collect(self, since: int) -> Window: ...

    def declared(self) -> frozenset[str] | None: ...
