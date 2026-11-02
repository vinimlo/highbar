"""Tests for the terminal reporter's marks and summary line."""

from __future__ import annotations

import pytest
from rich.console import Console

# Aliased: pytest would try to collect a name starting with "Test" as a test class.
from highbar.domain.models import RunSummary, Status, Usage
from highbar.domain.models import TestResult as Result
from highbar.engine.usage import add_up
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


def _with(*usages: Usage | None) -> RunSummary:
    totals = add_up(usages)
    return RunSummary(total=1, passed=1, failed=0, errored=0, duration_ms=500.0, totals=totals)


class TestUsage:
    COUNTED = Usage(input_tokens=3000, output_tokens=300, cost=0.0123, currency="USD")

    def test_ac4_the_summary_ends_with_tokens_and_cost(self) -> None:
        summary = _with(
            Usage(
                input_tokens=3000,
                output_tokens=300,
                cached_input_tokens=800,
                cost=0.0123,
                currency="USD",
            )
        )

        assert format_summary(summary) == (
            "1 passed | 0.50s | 3000 input tokens (800 cached) | 300 output tokens | 0.0123 USD"
        )

    def test_ac1_a_run_without_usage_shows_none(self) -> None:
        assert format_summary(_with(None)) == "1 passed | 0.50s"

    def test_a_small_cost_is_plain_digits(self) -> None:
        summary = _with(Usage(input_tokens=1, output_tokens=1, cost=0.000012, currency="USD"))

        assert "0.000012 USD" in format_summary(summary)

    def test_ac5_a_partial_sum_says_it_is_incomplete(self) -> None:
        assert format_summary(_with(self.COUNTED, None)).endswith("0.0123 USD | incomplete")

    def test_ac5_each_currency_shows_its_own_sum(self) -> None:
        euro = Usage(input_tokens=1, output_tokens=1, cost=1.5, currency="EUR")

        assert "1.5 EUR + 0.0123 USD | incomplete" in format_summary(_with(self.COUNTED, euro))

    def test_ac5_the_terminal_says_what_is_missing(
        self, reporter: TerminalReporter, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reporter.on_run_end(_with(self.COUNTED, None))

        output = capsys.readouterr().out
        assert "incomplete" in output
        assert "! incomplete sum: 1 of 2 turns with no usage" in output

    def test_a_complete_sum_warns_of_nothing(
        self, reporter: TerminalReporter, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reporter.on_run_end(_with(self.COUNTED))

        assert "incomplete" not in capsys.readouterr().out

    def test_a_currency_in_brackets_is_shown_and_not_read_as_a_style(
        self, reporter: TerminalReporter, capsys: pytest.CaptureFixture[str]
    ) -> None:
        usage = Usage(input_tokens=1, output_tokens=1, cost=1.0, currency="[red]")

        reporter.on_run_end(_with(usage))

        assert "1.0 [red]" in capsys.readouterr().out
