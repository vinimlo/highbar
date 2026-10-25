"""Highbar CLI — typer application with run, validate, and init commands."""

from __future__ import annotations

import signal
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import FrameType

import typer
from rich.console import Console
from rich.markup import escape

from highbar.adapters.subprocess_adapter import AgentStartError
from highbar.domain.models import RunSummary, Spec
from highbar.engine.observation import AgentNotObservedError
from highbar.engine.parser import parse_spec
from highbar.engine.run_spec import run_spec
from highbar.engine.schema import SpecError
from highbar.reporters.terminal import format_summary
from highbar.variations.cache import CacheError, VariationCache, find_cache_path, fingerprint
from highbar.variations.generator import VariationError, VariationGenerator

# Exit codes. CI tells "the agent misbehaved" (1) from "nothing could be checked" (2):
# a broken spec or an agent that never came up is a different fix from a red test.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNRUNNABLE = 2
EXIT_INTERRUPTED = 130  # the shell's code for a run stopped by Ctrl-C (128 + SIGINT)

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
    keep_traces: bool = typer.Option(
        False,
        "--keep-traces",
        help="Keep each spec's trace file (JSONL of the tool calls seen) and print its path.",
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
    current = ""
    try:
        with _terminate_as_exit():
            for current, spec in specs:
                summaries.append(
                    run_spec(
                        current,
                        spec,
                        generator=generator,
                        keep_traces=keep_traces,
                        console=console,
                    )
                )
    except AgentStartError as e:
        # The next specs are not run: an agent that cannot start is not a test result,
        # and a second agent on the same machine usually fails the same way.
        console.print(
            f"  [red]\u2717[/red] {escape(current)}: could not start the agent: {escape(str(e))}",
            soft_wrap=True,
        )
        raise typer.Exit(EXIT_UNRUNNABLE) from None
    except AgentNotObservedError as e:
        # Nothing could be checked: the agent is up but not instrumented (or not ours).
        console.print(f"  [red]\u2717[/red] {escape(current)}: {escape(str(e))}", soft_wrap=True)
        raise typer.Exit(EXIT_UNRUNNABLE) from None
    except KeyboardInterrupt:
        # The runner's finally has already stopped the agent's process group.
        console.print("\n  Interrupted.")
        raise typer.Exit(EXIT_INTERRUPTED) from None

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


@contextmanager
def _terminate_as_exit() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into SystemExit(128 + signal) while a run is going.

    `timeout`, closing the terminal and cancelling a CI job send these. Uncaught, they
    end the process without running a single `finally`: the agent (in its own process
    group) would stay on the port and the trace file in the temp directory.
    """

    def exit_on(signum: int, frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, exit_on) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


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
