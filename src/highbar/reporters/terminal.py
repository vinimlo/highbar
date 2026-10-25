"""Terminal reporter — rich-formatted test output."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from highbar.domain.models import RunSummary, Status, Suite, TestResult

# Mark and color per outcome. An error gets its own mark: it is neither a pass nor a
# failure of the agent, and reading it as either is the mistake this is here to stop.
_MARKS = {
    Status.PASSED: ("green", "✓"),
    Status.FAILED: ("red", "✗"),
    Status.ERROR: ("yellow", "!"),
}


def format_summary(summary: RunSummary, *, styled: bool = False) -> str:
    """``N passed | N failed | N error | N variations | 1.23s``, without zero counts.

    ``passed`` is always shown, so a run where nothing passed says so. ``styled`` adds
    Rich markup with the same colors as the marks.
    """
    parts = [(f"{summary.passed} passed", "green")]
    if summary.failed:
        parts.append((f"{summary.failed} failed", "red"))
    if summary.errored:
        parts.append((f"{summary.errored} error", "yellow"))
    if summary.variations:
        # Executions, not tests: a test with 5 variations still counts as one test.
        parts.append((f"{summary.variations} variations", "dim"))
    parts.append((f"{summary.duration_ms / 1000:.2f}s", ""))
    if not styled:
        return " | ".join(text for text, _ in parts)
    return " | ".join(f"[{style}]{text}[/{style}]" if style else text for text, style in parts)


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
        color, mark = _MARKS[result.status]
        self._console.print(f"    [{color}]{mark}[/{color}] {test_name}  [dim]{duration}[/dim]")
        for error in result.errors:
            self._console.print(f"      [{color}]→ {escape(error)}[/{color}]")

    def on_warning(self, message: str) -> None:
        self._console.print(f"  [yellow]! {escape(message)}[/yellow]", soft_wrap=True)

    def on_run_end(self, summary: RunSummary) -> None:
        self._console.print()
        self._console.print(f"  {format_summary(summary, styled=True)}")
        self._console.print()
