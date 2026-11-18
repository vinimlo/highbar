"""Highbar CLI — typer application with run, validate, and init commands."""

from __future__ import annotations

import math
import os
import signal
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import replace
from functools import partial
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer
from rich.markup import escape

from highbar.adapters.subprocess_adapter import AgentStartError
from highbar.cli.scaffold import quickstart_files
from highbar.domain.models import BUDGET_EXCEEDED, REPEATED_ERRORS, RunSummary, Spec
from highbar.engine.observation import AgentNotObservedError
from highbar.engine.parser import parse_spec
from highbar.engine.run_spec import load_session_hooks, run_spec
from highbar.engine.runs import check_runs
from highbar.engine.schema import SpecError
from highbar.engine.selection import SelectionError, select, selection_of
from highbar.engine.session import SessionError, SessionHooks
from highbar.engine.streak import DEFAULT_MAX_ERRORS, ErrorStreak
from highbar.engine.usage import add_costs, add_up
from highbar.export.braintrust import (
    DEFAULT_API_URL,
    DEFAULT_KEY_ENV,
    Client,
    ExportFailed,
    KeyRefused,
    ReportRefused,
    build_events,
    experiment_metadata,
    experiment_name,
    export,
    partial_note,
    read_key,
    read_report,
    url_problem,
)
from highbar.reporters.json_report import JsonReport, ReportError
from highbar.reporters.junit_report import JUnitReport
from highbar.reporters.markdown_report import MarkdownReport
from highbar.reporters.reports import Reports
from highbar.reporters.terminal import format_summary, new_console
from highbar.variations.cache import CacheError, VariationCache, find_cache_path, fingerprint
from highbar.variations.generator import VariationError, VariationGenerator

# Exit codes. CI tells "the agent misbehaved" (1) from "nothing could be checked" (2):
# a broken spec or an agent that never came up is a different fix from a red test.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNRUNNABLE = 2
EXIT_BUDGET_EXCEEDED = 3  # --max-cost stopped the run: the tests left say nothing either way
# --max-errors stopped the run: the agent's infrastructure failed, and nothing was measured.
# The message that stops the run states it (highbar.engine.streak.EXIT_CODE).
EXIT_REPEATED_ERRORS = 4
EXIT_INTERRUPTED = 130  # the shell's code for a run stopped by Ctrl-C (128 + SIGINT)
EXIT_SIGNALED = 128  # plus the signal's number

app = typer.Typer(
    name="highbar",
    help="Behavior testing framework for AI agents.",
    no_args_is_help=True,
)
console = new_console()


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
        totals=add_up(usage for s in summaries for test in s.tests for usage in test.usages),
    )


# Options that can be given more than once take a list, so they are built here and not
# in the signature (ruff B008), as typer documents.
_SUITE = typer.Option(
    None,
    "--suite",
    metavar="NAME",
    help=(
        "Run only the suites with this name. Given more than once, any of them; with -k "
        "or --tag, a test must match each."
    ),
)
_TAG = typer.Option(
    None,
    "--tag",
    metavar="NAME",
    help=(
        "Run only the tests with this tag, their suite's or their own. Given more than "
        "once, any of them; with --suite or -k, a test must match each."
    ),
)
_KEYWORD = typer.Option(
    None,
    "-k",
    metavar="TEXT",
    help=(
        "Run only the tests whose <suite>/<name> or id contains TEXT, ignoring case. "
        "Given more than once, any of them; with --suite or --tag, a test must match each."
    ),
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
        help=(
            "Run each phrasing N times, in place of the specs' defaults.runs; a test's own "
            "runs still wins. With min_pass, a flaky test becomes a gate."
        ),
    ),
    # Annotated, unlike the options around it: a list default in the signature is mutable.
    report_to: Annotated[
        list[str] | None,
        typer.Option(
            "--report",
            metavar="json=FILE|junit=FILE|markdown=FILE",
            help=(
                "Also write the results to FILE, as JSON, as JUnit XML or as a Markdown "
                "summary (for $GITHUB_STEP_SUMMARY). Give it once per file. Each is replaced "
                "before the first message, and written on every way out of the run."
            ),
        ),
    ] = None,
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        metavar="AMOUNT",
        help=(
            "Stop the run at the end of the test in progress once its costs add up to more "
            f"than AMOUNT, in the currency they came in. Exits with {EXIT_BUDGET_EXCEEDED}."
        ),
    ),
    max_errors: int = typer.Option(
        DEFAULT_MAX_ERRORS,
        "--max-errors",
        metavar="N",
        help=(
            "Stop the run once N executions in a row fail for the same cause of the "
            "agent's infrastructure: the same HTTP status, a refused connection, a timeout "
            "or an empty body, from the same endpoint. 0 never stops. Exits with "
            f"{EXIT_REPEATED_ERRORS}."
        ),
    ),
    jobs: int = typer.Option(
        1,
        "--jobs",
        metavar="N",
        help=(
            "Run up to N tests at once, each with its own messages in order. Every trace "
            "record must then carry agent.correlate: a test whose records have none is an "
            "error. Results are shown in spec order."
        ),
    ),
    suite: list[str] | None = _SUITE,
    keyword: list[str] | None = _KEYWORD,
    tag: list[str] | None = _TAG,
) -> None:
    """Run behavior tests against your agent."""
    paths = _discover_specs(spec_path)
    report = _open_report(report_to, paths)
    try:
        code = _run_all(
            paths,
            report,
            runs=runs,
            frozen=frozen,
            keep_traces=keep_traces,
            max_cost=max_cost,
            max_errors=max_errors,
            jobs=jobs,
            suites=suite or (),
            keywords=keyword or (),
            tags=tag or (),
        )
    except KeyboardInterrupt:
        # Anywhere in the run, parsing and hooks included. If an agent was up, the
        # runner's finally has already stopped its process group.
        console.print("\n  Interrupted.")
        code = _stopped(report, "interrupted", "stopped by Ctrl-C", EXIT_INTERRUPTED)
    except SystemExit as e:
        # SIGTERM or SIGHUP, from _terminate_as_exit, or rich when the reader closed
        # stdout: the agent is already down.
        code = e.code if isinstance(e.code, int) else EXIT_FAILED
        if report is not None:
            signaled = code > EXIT_SIGNALED
            message = f"stopped by signal {code - EXIT_SIGNALED}" if signaled else "output closed"
            report.stop("interrupted", message)
        raise SystemExit(_close_report(report, code)) from None
    except Exception as e:
        # A bug in highbar is not a test result. It ends as it always did, with its
        # traceback and a 1, and the file is what tells it from a failed test.
        if report is not None:
            report.stop("crashed", f"{type(e).__name__}: {e}")
            _close_report(report, EXIT_FAILED)
        raise
    raise typer.Exit(_close_report(report, code))


# The formats of --report: the same report, for a script (JSON), for the CI tools that
# read JUnit XML, and for a CI job's summary page (Markdown).
_REPORTS: dict[str, type[JsonReport]] = {
    "json": JsonReport,
    "junit": JUnitReport,
    "markdown": MarkdownReport,
}


def _open_report(values: list[str] | None, paths: list[str]) -> JsonReport | None:
    """The report of this run, already on disk, or exit 2 before anything starts.

    Writing it now proves the paths work before an hour of tests, and replaces older
    reports: a run that dies must not leave someone else's green at those paths.
    """
    if not values:
        return None
    specs = {Path(spec).resolve() for spec in paths}
    parts: list[JsonReport] = []
    for value in values:
        kind, separator, target = value.partition("=")
        if kind not in _REPORTS or not separator or not target:
            console.print(
                "  [red]\u2717[/red] --report takes json=<file>, junit=<file> or markdown=<file>, "
                f"got {escape(value)}"
            )
            raise typer.Exit(EXIT_UNRUNNABLE)
        path = Path(target)
        if path.resolve() in specs:
            console.print(f"  [red]\u2717[/red] --report: {escape(target)} is a spec of this run")
            raise typer.Exit(EXIT_UNRUNNABLE)
        if path.resolve() in {part.path.resolve() for part in parts}:
            console.print(f"  [red]\u2717[/red] --report: {escape(target)} is given twice")
            raise typer.Exit(EXIT_UNRUNNABLE)
        parts.append(_REPORTS[kind](path, version=_version()))
    report = parts[0] if len(parts) == 1 else Reports(parts, version=_version())
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
        _last_line(f"  [red]\u2717[/red] {escape(str(e))}")
        return EXIT_UNRUNNABLE
    _last_line(f"  Report: {escape(', '.join(str(path) for path in report.paths))}\n")
    return code


def _last_line(message: str) -> None:
    """Print after the verdict. rich exits 1 on a closed stdout, and the code is decided."""
    with suppress(SystemExit):
        console.print(message, soft_wrap=True)


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
    max_cost: float | None = None,
    max_errors: int = DEFAULT_MAX_ERRORS,
    jobs: int = 1,
    suites: Sequence[str] = (),
    keywords: Sequence[str] = (),
    tags: Sequence[str] = (),
) -> int:
    """Run every spec and return the exit code.

    It returns the code rather than raising typer.Exit, which is a RuntimeError and
    would read as a crash on its way out.
    """
    # Here and not in the option: the file has to be replaced before this exit too.
    if runs is not None and runs < 1:
        console.print("[red]--runs must be at least 1.[/red]")
        return _stopped(report, "usage_error", "--runs must be at least 1.", EXIT_UNRUNNABLE)

    if max_cost is not None and not 0 <= max_cost < math.inf:
        message = "--max-cost must be a number of at least 0."
        console.print(f"[red]{message}[/red]")
        return _stopped(report, "usage_error", message, EXIT_UNRUNNABLE)

    if max_errors < 0:
        message = "--max-errors must be a whole number of at least 0 (0 never stops)."
        console.print(f"[red]{message}[/red]")
        return _stopped(report, "usage_error", message, EXIT_UNRUNNABLE)

    if jobs < 1:
        message = "--jobs must be a whole number of at least 1."
        console.print(f"[red]{message}[/red]")
        return _stopped(report, "usage_error", message, EXIT_UNRUNNABLE)

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
    # Before the selection: a spec left out still lends its ${VAR} values to the masks.
    specs = _sharing_secrets(specs)
    # --suite, -k and --tag: the specs are whole above, so a typo anywhere still stops it.
    try:
        picked = select(specs, suites=suites, keywords=keywords, tags=tags)
    except SelectionError as e:
        console.print(f"  [red]\u2717[/red] {escape(str(e))}", soft_wrap=True)
        return _stopped(report, "usage_error", str(e), EXIT_UNRUNNABLE)
    selection = selection_of(specs, picked, suites=suites, keywords=keywords, tags=tags)
    specs = picked
    # After the selection: a spec left out starts no agent and no proxy.
    proxied = [path for path, spec in specs if spec.agent.llm_proxy]
    if jobs > 1 and proxied:
        message = (
            f"--jobs {jobs} cannot run with agent.llm_proxy ({', '.join(proxied)}): the "
            "agent's model calls carry no test id yet, so with more than one test in "
            "flight the proxy could not tell whose call it saw. Run it with --jobs 1"
        )
        console.print(f"[red]{escape(message)}[/red]")
        return _stopped(report, "usage_error", message, EXIT_UNRUNNABLE)
    if report is not None:
        report.plan(specs, selection)
    # Python session hooks are imported before any agent starts, like the specs parsed.
    hooks = _load_all_hooks(specs, failures)
    if hooks is None:
        return _stopped(report, "hooks_invalid", "; ".join(failures), EXIT_UNRUNNABLE)

    # One cache for the whole run. It reads its file lazily, so specs that ask for no
    # variations never touch it.
    generator = VariationGenerator(VariationCache(find_cache_path()), frozen=frozen)

    summaries: list[RunSummary] = []
    # The errors in a row so far: like the ceiling, the count is the whole run's.
    streak = ErrorStreak()
    current = ""
    try:
        with _terminate_as_exit():
            for index, (current, spec) in enumerate(specs):
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
                        max_cost=max_cost,
                        spent=_spent(summaries),
                        spent_untagged=_spent_untagged(summaries),
                        max_errors=max_errors or None,
                        streak=streak,
                        jobs=jobs,
                        # A stop leaves the specs after this one out (HB-33).
                        more_after=index < len(specs) - 1,
                    )
                )
                # Either stop is the whole run's, so the specs left do not start either.
                if summaries[-1].stopped is not None:
                    break
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
    except SessionError as e:
        # Only agent.run's setup gets here (RunHookError, HB-10): a session's own hooks
        # fail one execution, inside the run. Nothing was sent, and the specs left would
        # find what it was to set up missing too.
        message = f"{current}: {e}; nothing was sent"
        console.print(f"  [red]\u2717[/red] {escape(message)}", soft_wrap=True)
        return _stopped(report, "run_setup_failed", message, EXIT_UNRUNNABLE)

    total = _total(summaries)
    if len(summaries) > 1:
        console.print(f"  Total: {format_summary(total, styled=True)}\n")
    last = summaries[-1] if summaries else None
    # A run of part of the tests can be all green: the last lines say the rest did not run.
    # A stop's own unrun tests are counted apart: its message, below, says why.
    if selection is not None:
        stopped = last is not None and last.stopped is not None
        line = selection.line(not_run=selection.picked - total.total if stopped else 0)
        if line is not None:
            console.print(f"  [yellow]![/yellow] {escape(line)}", soft_wrap=True)

    # Before the tests' verdict: a run that was cut short is not a verdict on the agent.
    # The message is the runner's, with every value from ${VAR} already masked.
    if last is not None and last.stopped is not None:
        console.print(f"  [red]✗[/red] {escape(last.stopped)}", soft_wrap=True)
        if last.stop_reason == REPEATED_ERRORS:
            return _stopped(report, REPEATED_ERRORS, last.stopped, EXIT_REPEATED_ERRORS)
        return _stopped(report, BUDGET_EXCEEDED, last.stopped, EXIT_BUDGET_EXCEEDED)

    # An error is not a pass: a run where a test could not be checked is not green.
    return EXIT_FAILED if total.failed or total.errored else EXIT_OK


def _spent(summaries: list[RunSummary]) -> dict[str, float]:
    """What the specs that finished spent, per currency, as --max-cost counts it."""
    totals = _total(summaries).totals
    return add_costs(totals.cost if totals is not None else {}, _spent_untagged(summaries))


def _spent_untagged(summaries: list[RunSummary]) -> dict[str, float]:
    """The part of that with no test context, which no total shows."""
    return add_costs(*(summary.untagged_cost for summary in summaries))


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
                # A conversation's paraphrases replace its last turn, and are made with
                # what the user said before it (HB-16).
                if test.turns:
                    sentence = test.turns[-1].trigger
                    context = [turn.trigger for turn in test.turns[:-1]]
                else:
                    sentence, context = test.trigger or "", []
                if not test.variations or not sentence:
                    continue

                # Triggers and paraphrases are user text: escaped, or Rich would read
                # anything in square brackets as a style tag and drop it.
                trigger = escape(sentence)
                key = fingerprint(sentence, test.variations, context)
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

                    produced = generator.generate(sentence, test.variations, context=context)
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
def compare(
    base: Annotated[
        list[str],
        typer.Option("--base", help="A JSON report, or a folder of them: each one is a round."),
    ],
    head: Annotated[
        list[str],
        typer.Option("--head", help="The same, for the side under test."),
    ],
    fail_on_regression: Annotated[
        bool,
        typer.Option(
            "--fail-on-regression",
            help=(
                "Exit 1 when a test is worse: Holm p under 0.05, "
                "2+ rounds a side, rounds that agree."
            ),
        ),
    ] = False,
    report: Annotated[
        str | None,
        typer.Option("--report", help="json=FILE: the comparison as JSON, for CI."),
    ] = None,
) -> None:
    """Compare two groups of runs (--report json= files) test by test."""
    # Imported here, so `run` does not load what only `compare` uses.
    from highbar.compare.core import CompareError
    from highbar.compare.core import compare as compare_rounds
    from highbar.compare.output import load_side, render, write_json

    if report is not None and not report.startswith("json="):
        console.print("[red]--report takes json=FILE for compare.[/red]")
        raise typer.Exit(EXIT_UNRUNNABLE)
    try:
        seen: dict[str, Path] = {}
        rows = compare_rounds(load_side(base, seen), load_side(head, seen))
    except CompareError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]", soft_wrap=True)
        raise typer.Exit(EXIT_UNRUNNABLE) from exc
    render(rows, console)
    if report is not None:
        write_json(report.removeprefix("json="), rows, base, head)
    worse = any(row.verdict == "worse" for row in rows)
    raise typer.Exit(EXIT_FAILED if fail_on_regression and worse else EXIT_OK)


export_app = typer.Typer(
    help="Send a finished run's results (a --report json= file) to another tool.",
    no_args_is_help=True,
)
app.add_typer(export_app, name="export")


@export_app.command("braintrust")
def export_braintrust(
    report_path: Annotated[
        str, typer.Argument(metavar="REPORT", help="A --report json= file of a finished run.")
    ],
    project: Annotated[
        str, typer.Option("--project", help="The Braintrust project; created if it is not there.")
    ],
    experiment: Annotated[
        str | None,
        typer.Option(
            "--experiment",
            help="The experiment to send to. Default: the report's file name and start time.",
        ),
    ] = None,
    api_key_env: Annotated[
        str,
        typer.Option("--api-key-env", metavar="VAR", help="The variable that holds the API key."),
    ] = DEFAULT_KEY_ENV,
    api_url: Annotated[
        str, typer.Option("--api-url", metavar="URL", help="The Braintrust API to send to.")
    ] = DEFAULT_API_URL,
    partial: Annotated[
        bool,
        typer.Option(
            "--partial",
            help="Export a run that stopped before every test ran (complete: false), "
            "marked as partial. Without it, such a report exits 2.",
        ),
    ] = False,
) -> None:
    """Send a run's results to Braintrust: one experiment event per execution."""
    path = Path(report_path)
    try:
        body = read_report(path, partial=partial)
        events = build_events(body)
        if not project.strip():
            raise ReportRefused("--project needs the name of a Braintrust project")
        if (problem := url_problem(api_url)) is not None:
            raise ReportRefused(problem)
        key = read_key(api_key_env, os.environ.get(api_key_env))
    except (ReportRefused, KeyRefused) as e:
        console.print(f"  [red]\u2717[/red] {escape(str(e))}", soft_wrap=True)
        raise typer.Exit(EXIT_UNRUNNABLE) from None
    name = experiment or experiment_name(path, body)
    client = Client(api_url, key, api_key_env)
    try:
        export(events, client, project=project, experiment=name, metadata=experiment_metadata(body))
    except ExportFailed as e:
        console.print(
            f"  [red]\u2717[/red] Braintrust: {escape(e.reason)}. {e.sent} of {e.total} events "
            f"sent, {e.total - e.sent} not sent; sending again is safe, each event keeps its id",
            soft_wrap=True,
        )
        raise typer.Exit(EXIT_FAILED) from None
    finally:
        client.close()
    scores = [event["scores"]["highbar"] for event in events]
    console.print(
        f"  Sent {len(events)} events to Braintrust, project {escape(project)}, experiment "
        f"{escape(name)}: {scores.count(1)} passed, {scores.count(0)} failed, "
        f"{scores.count(None)} error (score null).",
        soft_wrap=True,
    )
    if (note := partial_note(body)) is not None:
        console.print(f"  [yellow]![/yellow] {escape(note)}.", soft_wrap=True)


@app.command()
def redteam(
    spec_path: Annotated[str, typer.Argument(help="The spec whose agent the attacks target.")],
    allow: Annotated[
        list[str] | None,
        typer.Option("--allow", help="A tool an attack may make the agent call. Repeatable."),
    ] = None,
    secret: Annotated[
        list[str] | None,
        typer.Option(
            "--secret",
            help="Text no reply may hold, such as a line of the system prompt. "
            "Written into the file, and shown when a test fails. Repeatable.",
        ),
    ] = None,
    lang: Annotated[
        list[str] | None,
        typer.Option("--lang", help="Only these languages: en, pt, es. Repeatable."),
    ] = None,
    out: Annotated[
        str | None,
        typer.Option("--out", help="Where to write it. Default: next to the spec."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Write over an attack file already there.")
    ] = False,
) -> None:
    """Write a spec of attacks on the agent's guardrails; run it with `highbar run`."""
    from highbar.redteam import AttackFileExists, write_attacks

    try:
        target = write_attacks(
            Path(spec_path),
            allow=allow or (),
            secrets=secret or (),
            languages=lang,
            out=Path(out) if out else None,
            force=force,
        )
    except (SpecError, OSError, ValueError) as exc:
        if isinstance(exc, AttackFileExists):
            message = (
                f"{exc} is already there, and may hold your edits: "
                "pass --force to write it again, or --out for another file."
            )
        else:
            message = str(exc)
        console.print(f"[red]{escape(message)}[/red]", soft_wrap=True)
        raise typer.Exit(EXIT_UNRUNNABLE) from exc
    count = len(parse_spec(str(target)).suites[0].tests)
    console.print(f"  Wrote {count} attacks to {escape(str(target))}.", soft_wrap=True)
    console.print(
        f"  Run them with: [bold]highbar run {escape(str(target))}[/bold]", soft_wrap=True
    )


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


@app.command()
def vacuum(
    spec_path: str = typer.Argument(
        default="",
        help="Spec file or directory. If omitted, all specs in specs/",
    ),
) -> None:
    """Find tests that an agent doing nothing useful would pass (no agent runs)."""
    from highbar.engine.vacuum import probe_spec

    paths = _discover_specs(spec_path)
    if not paths:
        console.print("[red]No spec files found.[/red]")
        raise typer.Exit(EXIT_UNRUNNABLE)
    specs = _parse_all(paths)
    if specs is None:
        raise typer.Exit(EXIT_UNRUNNABLE)

    judged = vacuous = 0
    for path, spec in specs:
        console.print(f"\n  [bold]{escape(path)}[/bold]")
        for finding in probe_spec(spec):
            name = escape(f"{finding.suite} › {finding.test}")
            if finding.not_judged:
                console.print(
                    f"    [dim]· {name}  not judged: {finding.not_judged} is beyond "
                    "a trivial agent[/dim]",
                    soft_wrap=True,
                )
                continue
            judged += 1
            if finding.passes_on:
                vacuous += 1
                console.print(
                    f"    [red]✗[/red] {name}  passes on: {', '.join(finding.passes_on)}",
                    soft_wrap=True,
                )
            else:
                console.print(f"    [green]✓[/green] {name}", soft_wrap=True)
    console.print(f"\n  {vacuous} vacuous of {judged}", soft_wrap=True)
    if vacuous:
        console.print(
            "  A vacuous test is green on an agent that does nothing useful: add the "
            "call it must make, the arguments, or what the reply must not say.",
            soft_wrap=True,
        )
    raise typer.Exit(EXIT_FAILED if vacuous else EXIT_OK)


def _sharing_secrets(specs: list[tuple[str, Spec]]) -> list[tuple[str, Spec]]:
    """Each spec with the ${VAR} values of all: an agent can echo another spec's token."""
    secrets = {value: name for _, spec in specs for value, name in spec.agent.secrets.items()}
    return [
        (path, replace(spec, agent=replace(spec.agent, secrets=secrets))) for path, spec in specs
    ]


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
