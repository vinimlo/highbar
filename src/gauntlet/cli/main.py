"""Gauntlet CLI — typer application with run, validate, and init commands."""

from __future__ import annotations

import tempfile
import textwrap
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from gauntlet.adapters.file_collector import FileTraceCollector
from gauntlet.adapters.http_client import HTTPClient
from gauntlet.adapters.subprocess_adapter import SubprocessAdapter
from gauntlet.assertions.function_calls import FunctionCallsAssertion
from gauntlet.assertions.response import ResponseAssertion
from gauntlet.engine.parser import parse_spec
from gauntlet.engine.runner import TestRunner
from gauntlet.reporters.terminal import TerminalReporter
from gauntlet.variations.cache import CacheError, VariationCache, find_cache_path, fingerprint
from gauntlet.variations.generator import VariationError, VariationGenerator

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
        help=(
            "Path to a .gauntlet.yaml spec file, or a directory to search. "
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
        raise typer.Exit(1)

    try:
        _ver = version("gauntlet")
    except PackageNotFoundError:
        _ver = "dev"
    console.print(f"\n  [bold]Gauntlet v{_ver}[/bold]")

    # One cache for the whole run. It reads its file lazily, so specs that ask for no
    # variations never touch it.
    generator = VariationGenerator(VariationCache(find_cache_path()), frozen=frozen)

    any_failed = False
    for path in paths:
        console.print(f"  Running {escape(path)}")
        spec = parse_spec(path)

        with tempfile.NamedTemporaryFile(
            suffix=".jsonl", prefix="gauntlet-traces-", delete=False
        ) as traces_fd:
            traces_file = traces_fd.name

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
            assertions=[FunctionCallsAssertion(), ResponseAssertion()],
            reporter=reporter,
            settle_time=spec.agent.settle_time / 1000.0,
            variations=generator,
        )

        summary = runner.run(spec)
        if summary.failed > 0:
            any_failed = True

    if any_failed:
        raise typer.Exit(1)


@app.command()
def variations(
    spec_path: str = typer.Argument(
        default="",
        help="Spec file or directory to generate variations for. If omitted, all specs in specs/",
    ),
    force: bool = typer.Option(False, "--force", help="Regenerate even if already cached."),
) -> None:
    """Generate the paraphrases a spec asks for, and cache them.

    Run this once, commit the cache, and `gauntlet run` is deterministic and offline
    from then on. Generating on every run would test a different set of sentences each
    time — CI would go red on a commit that changed nothing.
    """
    paths = _discover_specs(spec_path)
    if not paths:
        console.print("[red]No spec files found.[/red]")
        raise typer.Exit(1)

    cache = VariationCache(find_cache_path())
    generator = VariationGenerator(cache)
    generated = 0
    reused = 0
    short = 0

    for path in paths:
        spec = parse_spec(path)
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
    """Find spec files to run: the given file, or every spec under the given directory."""
    if spec_path and not Path(spec_path).is_dir():
        return [spec_path]

    specs_dir = Path(spec_path or "specs")
    if specs_dir.exists():
        return sorted(str(p) for p in specs_dir.rglob("*.gauntlet.yaml"))

    return []


if __name__ == "__main__":
    app()
