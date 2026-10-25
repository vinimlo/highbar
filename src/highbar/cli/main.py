"""Highbar CLI — typer application with run, validate, and init commands."""

from __future__ import annotations

import tempfile
import textwrap
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from highbar.adapters.file_collector import FileTraceCollector
from highbar.adapters.http_client import HTTPClient
from highbar.adapters.subprocess_adapter import SubprocessAdapter
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import RunSummary, Spec
from highbar.engine.parser import parse_spec
from highbar.engine.runner import TestRunner
from highbar.engine.schema import SpecError
from highbar.reporters.terminal import TerminalReporter, format_summary
from highbar.variations.cache import CacheError, VariationCache, find_cache_path, fingerprint
from highbar.variations.generator import VariationError, VariationGenerator

# Exit codes. CI tells "the agent misbehaved" (1) from "nothing could be checked" (2):
# a broken spec or an agent that never came up is a different fix from a red test.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNRUNNABLE = 2

app = typer.Typer(
    name="highbar",
    help="Behavior testing framework for AI agents.",
    no_args_is_help=True,
)
console = Console()


def _version() -> str:
    try:
        return version("highbar")
    except PackageNotFoundError:
        return "dev"


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"highbar {_version()}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def _main(
    show_version: bool = typer.Option(
        False,
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    pass


def _total(summaries: list[RunSummary]) -> RunSummary:
    return RunSummary(
        total=sum(s.total for s in summaries),
        passed=sum(s.passed for s in summaries),
        failed=sum(s.failed for s in summaries),
        errored=sum(s.errored for s in summaries),
        duration_ms=sum(s.duration_ms for s in summaries),
        variations=sum(s.variations for s in summaries),
    )


@app.command()
def run(
    spec_path: str = typer.Argument(
        default="",
        help=(
            "Path to a .highbar.yaml spec file, or a directory to search. "
            "If omitted, discovers all specs in specs/"
        ),
    ),
    frozen: bool = typer.Option(
        False,
        "--frozen",
        help=(
            "Never generate variations: a test whose variations are not in the committed "
            "cache fails. Use it in CI."
        ),
    ),
) -> None:
    """Run behavior tests against your agent."""
    paths = _discover_specs(spec_path)
    if not paths:
        console.print("[red]No spec files found.[/red]")
        raise typer.Exit(EXIT_UNRUNNABLE)

    console.print(f"\n  [bold]Highbar v{_version()}[/bold]")

    # Every spec is parsed before any agent starts: a typo in the third file must not
    # cost the time of running the first two, nor leave half a run behind.
    specs = _parse_all(paths)
    if specs is None:
        raise typer.Exit(EXIT_UNRUNNABLE)

    # One cache for the whole run. It reads its file lazily, so specs that ask for no
    # variations never touch it.
    generator = VariationGenerator(VariationCache(find_cache_path()), frozen=frozen)

    summaries: list[RunSummary] = []
    for path, spec in specs:
        console.print(f"  Running {escape(path)}")

        with tempfile.NamedTemporaryFile(
            suffix=".jsonl", prefix="highbar-traces-", delete=False
        ) as traces_fd:
            traces_file = traces_fd.name

        adapter = SubprocessAdapter(
            start_cmd=spec.agent.start,
            endpoint=spec.agent.endpoint,
            log_path=_agent_log_path(path),
            health_url=spec.agent.health,
            startup_timeout=spec.agent.startup_timeout,
            env={"HIGHBAR_TRACES_URL": traces_file},
        )
        client = HTTPClient(endpoint=spec.agent.endpoint)
        collector = FileTraceCollector(traces_file)
        reporter = TerminalReporter()

        runner = TestRunner(
            adapter=adapter,
            client=client,
            collector=collector,
            assertions=[FunctionCallsAssertion(), ResponseAssertion()],
            reporter=reporter,
            settle_time=spec.agent.settle_time / 1000.0,
            variations=generator,
        )

        summaries.append(runner.run(spec))

    total = _total(summaries)
    if len(summaries) > 1:
        console.print(f"  Total: {format_summary(total, styled=True)}\n")

    # An error is not a pass: a run where a test could not be checked is not green.
    raise typer.Exit(EXIT_FAILED if total.failed or total.errored else EXIT_OK)


@app.command()
def variations(
    spec_path: str = typer.Argument(
        default="",
        help="Spec file or directory to generate variations for. If omitted, all specs in specs/",
    ),
    force: bool = typer.Option(False, "--force", help="Regenerate even if already cached."),
) -> None:
    """Generate the paraphrases a spec asks for, and cache them.

    Run this once, commit the cache, and `highbar run` is deterministic and offline
    from then on. Generating on every run would test a different set of sentences each
    time — CI would go red on a commit that changed nothing.
    """
    paths = _discover_specs(spec_path)
    if not paths:
        console.print("[red]No spec files found.[/red]")
        raise typer.Exit(EXIT_UNRUNNABLE)

    cache = VariationCache(find_cache_path())
    generator = VariationGenerator(cache)
    generated = 0
    reused = 0
    short = 0

    specs = _parse_all(paths)
    if specs is None:
        raise typer.Exit(EXIT_UNRUNNABLE)

    for _, spec in specs:
        for suite in spec.suites:
            for test in suite.tests:
                if not test.variations or not test.trigger:
                    continue

                # Triggers and paraphrases are user text: escaped, or Rich would read
                # anything in square brackets as a style tag and drop it.
                trigger = escape(test.trigger)
                key = fingerprint(test.trigger, test.variations)
                try:
                    if force:
                        cache.invalidate(key)

                    cached = cache.get(key)
                    if cached is not None:
                        reused += 1
                        if len(cached) < test.variations:
                            short += 1
                            console.print(
                                f"  [yellow]![/yellow] {trigger} [dim](cached with "
                                f"{len(cached)} of {test.variations} — --force to retry)[/dim]"
                            )
                        continue

                    produced = generator.generate(test.trigger, test.variations)
                except (VariationError, CacheError) as exc:
                    console.print(f"  [red]✗[/red] {trigger}\n    {escape(str(exc))}")
                    raise typer.Exit(1) from exc

                generated += 1
                if len(produced) < test.variations:
                    # Fewer distinct paraphrases than requested. Still usable, but the
                    # count has to be visible or the spec's N reads as coverage it isn't.
                    short += 1
                    console.print(
                        f"  [yellow]![/yellow] {trigger} "
                        f"[yellow]({len(produced)} of {test.variations} requested)[/yellow]"
                    )
                else:
                    console.print(f"  [green]✓[/green] {trigger} [dim]({len(produced)})[/dim]")
                for variation in produced:
                    console.print(f"      [dim]{escape(variation)}[/dim]")

    summary = f"\n  {generated} generated | {reused} already cached"
    if short:
        summary += f" | [yellow]{short} with fewer than requested[/yellow]"
    console.print(f"{summary} [dim]({escape(str(cache.path))} — commit it)[/dim]\n")


@app.command()
def validate(
    spec_path: str = typer.Argument(
        default="",
        help="Spec file or directory to validate. If omitted, all specs in specs/",
    ),
) -> None:
    """Validate specs without running tests."""
    paths = _discover_specs(spec_path)
    if not paths:
        console.print("[red]No spec files found.[/red]")
        raise typer.Exit(EXIT_UNRUNNABLE)

    specs = _parse_all(paths, report_valid=True)
    raise typer.Exit(EXIT_UNRUNNABLE if specs is None else EXIT_OK)


def _parse_all(paths: list[str], *, report_valid: bool = False) -> list[tuple[str, Spec]] | None:
    """Parse every spec, printing one line per invalid one. ``None`` if any is invalid."""
    specs: list[tuple[str, Spec]] = []
    invalid = False
    for path in paths:
        try:
            spec = parse_spec(path)
        except SpecError as e:
            # The message already starts with the file's path.
            console.print(f"  [red]\u2717[/red] {escape(str(e))}", soft_wrap=True)
            invalid = True
            continue
        specs.append((path, spec))
        if report_valid:
            tests = sum(len(s.tests) for s in spec.suites)
            console.print(
                f"  [green]\u2713[/green] Valid: {escape(path)} "
                f"({len(spec.suites)} suite(s), {tests} test(s))"
            )
    return None if invalid else specs


@app.command()
def init() -> None:
    """Scaffold a highbar.config.yaml and example spec."""
    specs_dir = Path("specs")
    specs_dir.mkdir(exist_ok=True)

    example = specs_dir / "example.highbar.yaml"
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

    console.print("\n  Edit the spec and run: [bold]highbar run specs/example.highbar.yaml[/bold]")


def _agent_log_path(spec_path: str) -> Path:
    """``.highbar/logs/<spec stem>-<timestamp>.log``, relative to where highbar runs."""
    stem = Path(spec_path).name.removesuffix(".yaml").removesuffix(".highbar")
    return Path(".highbar") / "logs" / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.log"


def _discover_specs(spec_path: str) -> list[str]:
    """Find spec files to run: the given file, or every spec under the given directory."""
    if spec_path and not Path(spec_path).is_dir():
        return [spec_path]

    specs_dir = Path(spec_path or "specs")
    if specs_dir.exists():
        return sorted(str(p) for p in specs_dir.rglob("*.highbar.yaml"))

    return []


if __name__ == "__main__":
    app()
