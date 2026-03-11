"""Gauntlet CLI — typer application with run, validate, and init commands."""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import typer
from rich.console import Console

from gauntlet.adapters.file_collector import FileTraceCollector
from gauntlet.adapters.http_client import HTTPClient
from gauntlet.adapters.subprocess_adapter import SubprocessAdapter
from gauntlet.assertions.function_calls import FunctionCallsAssertion
from gauntlet.assertions.response import ResponseAssertion
from gauntlet.assertions.result import ResultAssertion
from gauntlet.engine.parser import parse_spec
from gauntlet.engine.runner import TestRunner
from gauntlet.reporters.terminal import TerminalReporter

app = typer.Typer(
    name="gauntlet",
    help="Behavior testing framework for AI agents.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def run(
    spec_path: str = typer.Argument(
        default="",
        help="Path to a .gauntlet.yaml spec file. If omitted, discovers all specs in specs/",
    ),
) -> None:
    """Run behavior tests against your agent."""
    paths = _discover_specs(spec_path)
    if not paths:
        console.print("[red]No spec files found.[/red]")
        raise typer.Exit(1)

    console.print("\n  [bold]Gauntlet v0.1.0[/bold]")

    any_failed = False
    for path in paths:
        console.print(f"  Running {path}")
        spec = parse_spec(path)

        traces_file = tempfile.mktemp(suffix=".jsonl", prefix="gauntlet-traces-")

        adapter = SubprocessAdapter(
            start_cmd=spec.agent.start,
            health_url=spec.agent.health,
            startup_timeout=spec.agent.startup_timeout,
            env={"GAUNTLET_TRACES_URL": traces_file},
        )
        client = HTTPClient(endpoint=spec.agent.endpoint)
        collector = FileTraceCollector(traces_file)
        reporter = TerminalReporter()

        runner = TestRunner(
            adapter=adapter,
            client=client,
            collector=collector,
            assertions=[FunctionCallsAssertion(), ResponseAssertion(), ResultAssertion()],
            reporter=reporter,
            settle_time=spec.agent.settle_time / 1000.0,
        )

        summary = runner.run(spec)
        if summary.failed > 0:
            any_failed = True

    if any_failed:
        raise typer.Exit(1)


@app.command()
def validate(
    spec_path: str = typer.Argument(help="Path to a .gauntlet.yaml spec file"),
) -> None:
    """Validate spec syntax without running tests."""
    try:
        spec = parse_spec(spec_path)
        test_count = sum(len(s.tests) for s in spec.suites)
        suite_count = len(spec.suites)
        console.print(
            f"  [green]\u2713[/green] Valid: {suite_count} suite(s), {test_count} test(s)"
        )
    except Exception as e:
        console.print(f"  [red]\u2717[/red] Invalid: {e}")
        raise typer.Exit(1) from e


@app.command()
def init() -> None:
    """Scaffold a gauntlet.config.yaml and example spec."""
    specs_dir = Path("specs")
    specs_dir.mkdir(exist_ok=True)

    example = specs_dir / "example.gauntlet.yaml"
    if example.exists():
        console.print(f"  [yellow]Skipped[/yellow] {example} already exists")
    else:
        example.write_text(
            textwrap.dedent("""\
            agent:
              start: "python -m my_agent.server"
              endpoint: "http://localhost:8000/chat"
              health: "http://localhost:8000/health"

            suites:
              - name: Basic
                tests:
                  - trigger: "Hello!"
                    expect:
                      response:
                        contains: "hello"
        """)
        )
        console.print(f"  [green]Created[/green] {example}")

    console.print(
        "\n  Edit the spec and run: [bold]gauntlet run specs/example.gauntlet.yaml[/bold]"
    )


def _discover_specs(spec_path: str) -> list[str]:
    """Find spec files to run."""
    if spec_path:
        return [spec_path]

    specs_dir = Path("specs")
    if specs_dir.exists():
        return sorted(str(p) for p in specs_dir.rglob("*.gauntlet.yaml"))

    return []


if __name__ == "__main__":
    app()
