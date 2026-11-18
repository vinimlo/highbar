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

    def test_hb21_a_negative_control_shows_what_it_expected(
        self, reporter: TerminalReporter, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = Result(
            status=Status.PASSED,
            errors=["response does not contain 'ok'"],
            duration_ms=3.0,
            expected=Status.FAILED,
        )

        reporter.on_test_result("a tool that fails", result)

        output = capsys.readouterr().out
        assert "✓ a tool that fails (expected failed)" in output
        # What made it fail is in the report; the terminal keeps one line for a control.
        assert "does not contain" not in output

    def test_hb21_a_control_that_did_not_fail_shows_why(
        self, reporter: TerminalReporter, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mismatch = "passed, but expect_status is failed"
        result = Result(
            status=Status.FAILED, errors=[mismatch], duration_ms=3.0, expected=Status.FAILED
        )

        reporter.on_test_result("a tool that fails", result)

        output = capsys.readouterr().out
        assert "✗ a tool that fails (expected failed)" in output
        assert mismatch in output


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


class _Writes:
    """A file that keeps each write apart: a pipe sees them apart too."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class TestTheSummaryIsTheLastWrite:
    """HB-35: a reader that leaves at the summary line must not turn a 0 into a 1.

    rich exits 1 when a write finds the pipe closed. Anything written after the line
    with `passed`, in a write of its own, races the reader leaving: test_i2 failed in
    4 of 30 runs on develop, always on the blank line after the summary.
    """

    @pytest.mark.parametrize(
        "usages",
        [(TestUsage.COUNTED,), (TestUsage.COUNTED, None)],
        ids=["complete", "with-warnings"],
    )
    def test_hb35_nothing_is_written_after_the_summary_line(
        self, usages: tuple[Usage | None, ...]
    ) -> None:
        file = _Writes()
        reporter = TerminalReporter()
        reporter._console = Console(file=file, width=200, force_terminal=False)  # type: ignore[arg-type]

        reporter.on_run_end(_with(*usages))

        writes = [text for text in file.writes if text]
        summary = next(i for i, text in enumerate(writes) if "passed" in text)
        assert summary == len(writes) - 1, writes
        assert writes[-1].endswith("\n\n")

    def test_hb35_the_warnings_still_follow_the_summary(self) -> None:
        file = _Writes()
        reporter = TerminalReporter()
        reporter._console = Console(file=file, width=200, force_terminal=False)  # type: ignore[arg-type]

        reporter.on_run_end(_with(TestUsage.COUNTED, None))

        text = "".join(file.writes)
        assert text.index("1 passed") < text.index("! incomplete sum: 1 of 2 turns with no usage")


class TestDetails:
    def test_a_long_detail_wraps_under_its_own_text(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Wrapped at column 0, a long `Got: '...'` broke the indentation of the list."""
        reporter = TerminalReporter()
        reporter._console = Console(width=60, force_terminal=False)
        error = (
            "Response matches forbidden pattern '\\b4[0-9]{3}([ -]?[0-9]{4}){3}\\b' (found "
            "'4111 1111 1111 1111'). Got: 'Saved your card 4111 1111 1111 1111 for next time.'"
        )

        reporter.on_test_result("never repeats a card number", Result(Status.FAILED, [error], 5.0))

        lines = capsys.readouterr().out.splitlines()[1:]
        assert len(lines) > 1
        assert lines[0].startswith("      → Response matches")
        assert all(line.startswith(" " * 8) and line[8] != " " for line in lines[1:]), lines
        assert all(len(line) <= 60 and not line.endswith(" ") for line in lines), lines
        assert " ".join(line.strip().removeprefix("→ ") for line in lines) == error

    def test_square_brackets_in_a_detail_are_kept(
        self, reporter: TerminalReporter, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reporter.on_test_result("t", Result(Status.FAILED, ["Got: '[red]x[/red]'"], 5.0))

        assert "→ Got: '[red]x[/red]'" in capsys.readouterr().out


class TestColors:
    def test_numbers_and_paths_are_not_colored_by_rich(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Rich's highlighter colored half of "v0.2.1", a duration and a log's timestamp."""
        monkeypatch.setenv("FORCE_COLOR", "1")
        reporter = TerminalReporter()

        reporter.on_test_result("order 1042 at /tmp/a-20261118.log", Result(Status.PASSED, [], 5.0))

        assert "order 1042 at /tmp/a-20261118.log" in capsys.readouterr().out
