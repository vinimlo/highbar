"""Highbar CLI — typer application with run, validate, and init commands."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import FrameType

import typer
from rich.console import Console
from rich.markup import escape

from highbar.adapters.subprocess_adapter import AgentStartError
from highbar.cli.scaffold import quickstart_files
from highbar.domain.models import RunSummary, Spec
from highbar.engine.observation import AgentNotObservedError
from highbar.engine.parser import parse_spec
from highbar.engine.run_spec import load_session_hooks, run_spec
from highbar.engine.runs import check_runs
from highbar.engine.schema import SpecError
from highbar.engine.session import SessionError, SessionHooks
from highbar.reporters.json_report import JsonReport, ReportError
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
def _main(  # pyright: ignore[reportUnusedFunction]  # registered by the decorator
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
    runs: int | None = typer.Option(
        None,
        "--runs",
        min=1,
        help=(
            "Run each phrasing N times, in place of the specs' defaults.runs; a test's own "
            "runs still wins. With min_pass, a flaky test becomes a gate."
        ),
    ),
    report_to: str | None = typer.Option(
        None,
        "--report",
        metavar="json=FILE",
        help=(
            "Also write the results to FILE as JSON. It is replaced before the first "
            "message, and written on every way out of the run."
        ),
    ),
) -> None:
    """Run behavior tests against your agent."""
    paths = _discover_specs(spec_path)
    report = _open_report(report_to, paths)
    try:
        code = _run_all(paths, report, runs=runs, frozen=frozen, keep_traces=keep_traces)
    except SystemExit as e:
        # SIGTERM or SIGHUP, from _terminate_as_exit: the agent is already down.
        code = e.code if isinstance(e.code, int) else EXIT_FAILED
        if report is not None:
            report.stop("interrupted", f"stopped by signal {code - 128}")
        raise SystemExit(_close_report(report, code)) from None
    except Exception as e:
        # A bug in highbar is not a test result. It ends as it always did, with its
        # traceback and a 1, and the file is what tells it from a failed test.
        if report is not None:
            report.stop("crashed", f"{type(e).__name__}: {e}")
            _close_report(report, EXIT_FAILED)
        raise
    raise typer.Exit(_close_report(report, code))


def _open_report(value: str | None, paths: list[str]) -> JsonReport | None:
    """The report of this run, already on disk, or exit 2 before anything starts.

    Writing it now proves the path works before an hour of tests, and replaces an
    older report: a run that dies must not leave someone else's green at that path.
    """
    if value is None:
        return None
    kind, separator, target = value.partition("=")
    if kind != "json" or not separator or not target:
        console.print(f"  [red]\u2717[/red] --report takes json=<file>, got {escape(value)}")
        raise typer.Exit(EXIT_UNRUNNABLE)
    path = Path(target)
    if path.resolve() in {Path(spec).resolve() for spec in paths}:
        console.print(f"  [red]\u2717[/red] --report: {escape(target)} is a spec of this run")
        raise typer.Exit(EXIT_UNRUNNABLE)
    report = JsonReport(path, version=_version())
    try:
        report.start()
    except ReportError as e:
        console.print(f"  [red]\u2717[/red] {escape(str(e))}", soft_wrap=True)
        raise typer.Exit(EXIT_UNRUNNABLE) from None
    return report


def _close_report(report: JsonReport | None, code: int) -> int:
    """Write the final report. A CI job counts on the file, so losing it cannot be a 0."""
    if report is None:
        return code
    try:
        report.finish(code)
    except ReportError as e:
        console.print(f"  [red]\u2717[/red] {escape(str(e))}", soft_wrap=True)
        return EXIT_UNRUNNABLE
    console.print(f"  Report: {escape(str(report.path))}\n", soft_wrap=True)
    return code


def _stopped(report: JsonReport | None, reason: str, message: str, code: int) -> int:
    if report is not None:
        report.stop(reason, message)
    return code


def _run_all(
    paths: list[str],
    report: JsonReport | None,
    *,
    runs: int | None,
    frozen: bool,
    keep_traces: bool,
) -> int:
    """Run every spec and return the exit code.

    It returns the code rather than raising typer.Exit, which is a RuntimeError and
    would read as a crash on its way out.
    """
    if not paths:
        console.print("[red]No spec files found.[/red]")
        return _stopped(report, "no_specs", "No spec files found.", EXIT_UNRUNNABLE)

    console.print(f"\n  [bold]Highbar v{_version()}[/bold]")

    # Every spec is parsed before any agent starts: a typo in the third file must not
    # cost the time of running the first two, nor leave half a run behind.
    failures: list[str] = []
    specs = _parse_all(paths, runs=runs, failures=failures)
    if specs is None:
        return _stopped(report, "spec_invalid", "; ".join(failures), EXIT_UNRUNNABLE)
    if report is not None:
        report.plan(specs)
    # Python session hooks are imported before any agent starts, like the specs parsed.
    hooks = _load_all_hooks(specs, failures)
    if hooks is None:
        return _stopped(report, "hooks_invalid", "; ".join(failures), EXIT_UNRUNNABLE)

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
                        session=hooks[current],
                        default_runs=runs,
                        on_end=partial(report.add, current, spec) if report else None,
                    )
                )
    except AgentStartError as e:
        # The next specs are not run: an agent that cannot start is not a test result,
        # and a second agent on the same machine usually fails the same way.
        console.print(
            f"  [red]\u2717[/red] {escape(current)}: could not start the agent: {escape(str(e))}",
            soft_wrap=True,
        )
        message = f"{current}: could not start the agent: {e}"
        return _stopped(report, "agent_start_failed", message, EXIT_UNRUNNABLE)
    except AgentNotObservedError as e:
        # Nothing could be checked: the agent is up but not instrumented (or not ours).
        console.print(f"  [red]\u2717[/red] {escape(current)}: {escape(str(e))}", soft_wrap=True)
        return _stopped(report, "agent_not_observed", f"{current}: {e}", EXIT_UNRUNNABLE)
    except KeyboardInterrupt:
        # The runner's finally has already stopped the agent's process group.
        console.print("\n  Interrupted.")
        return _stopped(report, "interrupted", "stopped by Ctrl-C", EXIT_INTERRUPTED)

    total = _total(summaries)
    if len(summaries) > 1:
        console.print(f"  Total: {format_summary(total, styled=True)}\n")

    # An error is not a pass: a run where a test could not be checked is not green.
    return EXIT_FAILED if total.failed or total.errored else EXIT_OK


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

    specs = _parse_all(paths, report_valid=True, runs=None)
    raise typer.Exit(EXIT_UNRUNNABLE if specs is None else EXIT_OK)


def _load_all_hooks(
    specs: list[tuple[str, Spec]], failures: list[str]
) -> dict[str, SessionHooks | None] | None:
    """Each spec's session hooks, loaded. ``None`` (after one line each) if any fails."""
    hooks: dict[str, SessionHooks | None] = {}
    failed = False
    for path, spec in specs:
        try:
            hooks[path] = load_session_hooks(path, spec)
        except SessionError as e:
            console.print(f"  [red]\u2717[/red] {escape(path)}: {escape(str(e))}", soft_wrap=True)
            failures.append(f"{path}: {e}")
            failed = True
    return None if failed else hooks


def _parse_all(
    paths: list[str],
    *,
    report_valid: bool = False,
    runs: int | None | bool = False,
    failures: list[str] | None = None,
) -> list[tuple[str, Spec]] | None:
    """Parse every spec, printing one line per invalid one. ``None`` if any is invalid.

    Unless ``runs`` is False, each test's min_pass is checked against its runs: with
    ``runs`` (``--runs``) in place of the spec's defaults.runs when it is a number.
    """
    specs: list[tuple[str, Spec]] = []
    invalid = False
    for path in paths:
        try:
            spec = parse_spec(path)
            if runs is not False:
                try:
                    check_runs(spec, spec.default_runs if runs is None else int(runs))
                except SpecError as e:
                    raise SpecError(f"{path}: {e}") from None
        except SpecError as e:
            # The message already starts with the file's path.
            console.print(f"  [red]\u2717[/red] {escape(str(e))}", soft_wrap=True)
            if failures is not None:
                failures.append(str(e))
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
    """Write a quickstart agent and its spec here; run them with `highbar run`."""
    for name, content in quickstart_files().items():
        target = Path(name)
        if target.exists():
            console.print(f"  [yellow]Skipped[/yellow] {name} (already exists)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        console.print(f"  [green]Created[/green] {name}")

    console.print("\n  Next: [bold]highbar run[/bold]")
    console.print('  (the agent needs FastAPI and uvicorn: pip install "highbar\\[quickstart]")')


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
