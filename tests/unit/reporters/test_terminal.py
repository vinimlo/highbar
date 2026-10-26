"""Tests for the terminal reporter's marks and summary line."""

from __future__ import annotations

import pytest
from rich.console import Console

# Aliased: pytest would try to collect a name starting with "Test" as a test class.
from highbar.domain.models import RunSummary, Status
from highbar.domain.models import TestResult as Result
from highbar.reporters.terminal import TerminalReporter, format_summary


@pytest.fixture
def reporter() -> TerminalReporter:
    reporter = TerminalReporter()
    reporter._console = Console(width=200, force_terminal=False)
    return reporter


class TestMarks:
    def test_error_has_its_own_mark(
        self, reporter: TerminalReporter, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = Result(status=Status.ERROR, errors=["HTTP error: refused"], duration_ms=3.0)

        reporter.on_test_result("hello", result)

        output = capsys.readouterr().out
        assert "! hello" in output
        assert "HTTP error: refused" in output
        assert "✗" not in output


class TestSummary:
    def test_counts_the_three_outcomes_separately(self) -> None:
        summary = RunSummary(
            total=3, passed=1, failed=1, errored=1, duration_ms=1230.0, variations=2
        )

        assert format_summary(summary) == "1 passed | 1 failed | 1 error | 2 variations | 1.23s"

    def test_omits_zero_counts_except_passed(self) -> None:
        summary = RunSummary(total=1, passed=0, failed=1, errored=0, duration_ms=500.0)

        assert format_summary(summary) == "0 passed | 1 failed | 0.50s"
