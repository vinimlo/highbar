"""Terminal reporter — rich-formatted test output."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from gauntlet.domain.models import RunSummary, Suite, TestResult


class TerminalReporter:
    """Formats test results for terminal output using rich."""

    def __init__(self) -> None:
        self._console = Console()

    def on_suite_start(self, suite: Suite) -> None:
        self._console.print(f"\n  [bold]{escape(suite.name)}[/bold]")

    def on_test_result(self, test_name: str, result: TestResult) -> None:
        duration = f"{result.duration_ms:.0f}ms"
        # Names and errors carry user text — triggers, tool payloads, the
        # `[variation: "..."]` prefix. Unescaped, Rich reads anything in square brackets
        # as a style tag and silently drops it from the output.
        test_name = escape(test_name)
        if result.passed:
            self._console.print(f"    [green]\u2713[/green] {test_name}  [dim]{duration}[/dim]")
        else:
            self._console.print(f"    [red]\u2717[/red] {test_name}  [dim]{duration}[/dim]")
            for error in result.errors:
                self._console.print(f"      [red]\u2192 {escape(error)}[/red]")

    def on_run_end(self, summary: RunSummary) -> None:
        duration = f"{summary.duration_ms / 1000:.2f}s"
        self._console.print()

        parts: list[str] = []
        if summary.passed > 0:
            parts.append(f"[green]{summary.passed} passed[/green]")
        if summary.failed > 0:
            parts.append(f"[red]{summary.failed} failed[/red]")
        if summary.variations > 0:
            # Executions, not tests: a test with 5 variations still counts as one test.
            parts.append(f"[dim]{summary.variations} variations[/dim]")

        self._console.print(f"  {' | '.join(parts)} | {duration}")
        self._console.print()
