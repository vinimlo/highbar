"""Run one parsed spec against its agent: shared by the CLI and ``highbar.run()``."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console
from rich.markup import escape

from highbar.adapters.attached import AttachedAgent
from highbar.adapters.file_collector import (
    FileTraceCollector,
    NoTraceCollector,
    trace_collector,
)
from highbar.adapters.http_client import HTTPClient
from highbar.adapters.llm_proxy import LlmProxy, Upstream
from highbar.adapters.subprocess_adapter import AgentStartError, SubprocessAdapter
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import RunSummary, SessionConfig, Spec
from highbar.domain.protocols import Reporter, TraceCollector
from highbar.engine.logs import log_path
from highbar.engine.observation import AgentNotObservedError
from highbar.engine.runner import TestRunner
from highbar.engine.secrets import redact
from highbar.engine.session import SessionHooks
from highbar.engine.streak import ErrorStreak
from highbar.reporters.terminal import TerminalReporter
from highbar.variations.generator import VariationGenerator

# Called once per spec, however it ended: the result so far, and whether it ran to the end.
OnEnd = Callable[[RunSummary, bool], None]

# agent.llm_proxy (HB-30): the variable each SDK reads its base URL from, the provider's
# own base URL when it is unset, and the provider that URL is.
PROXIED = (
    ("openai", "OPENAI_BASE_URL", "https://api.openai.com/v1", "api.openai.com"),
    ("anthropic", "ANTHROPIC_BASE_URL", "https://api.anthropic.com", "api.anthropic.com"),
)


def proxy_upstreams(environ: Mapping[str, str]) -> dict[str, Upstream]:
    """Where the proxy sends each protocol: where the agent would have gone without it.

    ``provider`` is said only for the provider's own host: a name in the price table is
    looked up under it, and OpenRouter or DeepSeek on the OpenAI protocol are not OpenAI.
    """
    upstreams: dict[str, Upstream] = {}
    for name, variable, default, host in PROXIED:
        base = environ.get(variable) or default
        provider = name if urlsplit(base).hostname == host else None
        upstreams[name] = Upstream(base, protocol=name, provider=provider)  # type: ignore[arg-type]
    return upstreams


def load_session_hooks(path: str, spec: Spec) -> SessionHooks | None:
    """The spec's session and run hooks and its ``expect.check`` functions, with the
    Python ones imported.

    Raises ``SessionError``.
    """
    checks = sorted(
        {
            expect.check
            for suite in spec.suites
            for test in suite.tests
            for expect in [test.expect, *(turn.expect for turn in test.turns or [])]
            if expect is not None and expect.check is not None
        }
    )
    if spec.agent.session is None and spec.agent.run is None and not checks:
        return None
    hooks = SessionHooks(
        spec.agent.session or SessionConfig(),
        spec_path=path,
        timeout_ms=spec.agent.timeout,
        secrets=spec.agent.secrets,
        run=spec.agent.run,
        checks=checks,
    )
    hooks.load()
    return hooks


def run_spec(
    path: str,
    spec: Spec,
    *,
    generator: VariationGenerator,
    keep_traces: bool,
    console: Console,
    session: SessionHooks | None = None,
    default_runs: int | None = None,
    reporter: Reporter | None = None,
    on_end: OnEnd | None = None,
    max_cost: float | None = None,
    spent: Mapping[str, float] | None = None,
    spent_untagged: Mapping[str, float] | None = None,
    max_errors: int | None = None,
    streak: ErrorStreak | None = None,
    jobs: int = 1,
    more_after: bool = False,
) -> RunSummary:
    """Run one spec against its agent.

    Managed mode (``start``): highbar starts the agent with a new trace file in
    ``HIGHBAR_TRACES_URL``, stops it after, and removes the file unless kept.
    Attach mode: the agent is already running and writes ``traces`` itself; highbar
    neither stops it nor touches its file.

    ``reporter`` takes the place of the terminal output of each test. ``on_end`` gets
    the result on every way out, including a run that stopped halfway. ``max_cost`` is the
    ceiling of a whole run, of which earlier specs already ``spent`` that much per
    currency, ``spent_untagged`` of it with no test context: over it, the spec stops at
    the end of a test, and ``stopped`` says so. ``max_errors`` executions in a row that
    failed for one cause of the infrastructure stop it after the last of them, counted in
    ``streak``, which earlier specs of the run may have started (None or 0: no stop).
    ``jobs`` is how many tests may be in flight at once (HB-8).
    """
    console.print(f"  Running {escape(path)}")
    agent = spec.agent
    if agent.llm_proxy and jobs > 1:
        # The CLI refuses it first; a caller from Python gets the same answer.
        raise ValueError("agent.llm_proxy runs with jobs=1: model calls carry no test id yet")
    reporter = reporter or TerminalReporter()
    # The CLI's --runs, when given, takes the place of the spec's defaults.runs.
    runs = spec.default_runs if default_runs is None else default_runs
    client = HTTPClient(
        agent.endpoint,
        request=agent.request,
        response_path=agent.response,
        timeout_ms=agent.timeout,
        body_is_reply=agent.reply == "body",
    )

    if agent.start is None:
        collector: TraceCollector = (
            trace_collector(agent.traces) if agent.traces else NoTraceCollector()
        )
        runner = _runner(
            spec,
            path,
            adapter=AttachedAgent(agent.endpoint, agent.health),
            client=client,
            collector=collector,
            generator=generator,
            session=session,
            managed=False,
            default_runs=runs,
            reporter=reporter,
            max_cost=max_cost,
            spent=spent,
            spent_untagged=spent_untagged,
            max_errors=max_errors,
            streak=streak,
            jobs=jobs,
            more_after=more_after,
            proxy=None,
        )
        return _run(runner, spec, on_end)

    with tempfile.NamedTemporaryFile(
        suffix=".jsonl", prefix="highbar-traces-", delete=False
    ) as traces_fd:
        traces_file = traces_fd.name
    if keep_traces:
        # Said before the run, not after: after the summary, a write of its own races a
        # reader that leaves at the summary line, and rich exits 1 on it (HB-35).
        console.print(f"  Traces kept at {escape(traces_file)}")

    env = {"HIGHBAR_TRACES_URL": traces_file}
    proxy: LlmProxy | None = None
    if agent.llm_proxy:
        # Started before the agent, which reads its base URLs once, when it starts.
        proxy = LlmProxy(traces_file, proxy_upstreams(os.environ))
        proxy.start()
        env.update({variable: proxy.base_url(name) for name, variable, _, _ in PROXIED})
    adapter = SubprocessAdapter(
        start_cmd=agent.start,
        endpoint=agent.endpoint,
        log_path=agent_log_path(path),
        health_url=agent.health,
        startup_timeout=agent.startup_timeout,
        env=env,
    )
    runner = _runner(
        spec,
        path,
        adapter=adapter,
        client=client,
        collector=FileTraceCollector(traces_file),
        generator=generator,
        session=session,
        managed=True,
        default_runs=runs,
        reporter=reporter,
        max_cost=max_cost,
        spent=spent,
        spent_untagged=spent_untagged,
        max_errors=max_errors,
        streak=streak,
        jobs=jobs,
        more_after=more_after,
        proxy=proxy,
    )

    try:
        summary = _run(runner, spec, on_end)
    finally:
        # After the agent, which the runner stopped on every way out: a call it makes
        # while it stops still goes through, and is recorded.
        if proxy is not None:
            proxy.stop()
        if not keep_traces:
            Path(traces_file).unlink(missing_ok=True)

    if summary.failed or summary.errored:
        # What the agent printed while failing is usually the explanation.
        console.print(f"  Agent log: {escape(str(adapter.log_path))}\n", soft_wrap=True)
    return summary


def _runner(
    spec: Spec,
    path: str,
    *,
    adapter: SubprocessAdapter | AttachedAgent,
    client: HTTPClient,
    collector: TraceCollector,
    generator: VariationGenerator,
    session: SessionHooks | None,
    managed: bool,
    default_runs: int,
    reporter: Reporter,
    max_cost: float | None,
    spent: Mapping[str, float] | None,
    spent_untagged: Mapping[str, float] | None,
    max_errors: int | None,
    streak: ErrorStreak | None,
    jobs: int,
    more_after: bool,
    proxy: LlmProxy | None,
) -> TestRunner:
    return TestRunner(
        adapter=adapter,  # type: ignore[arg-type]
        client=client,
        collector=collector,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter,
        settle_time=spec.agent.settle_time / 1000.0,
        variations=generator,
        managed=managed,
        session=session,
        spec_path=path,
        correlate=spec.agent.correlate,
        secrets=spec.agent.secrets,
        default_runs=default_runs,
        max_cost=max_cost,
        reply_within=spec.agent.timeout / 1000 if spec.agent.reply == "turn" else None,
        hook_timeout=spec.agent.timeout / 1000,
        spent=spent,
        spent_untagged=spent_untagged,
        max_errors=max_errors,
        streak=streak,
        jobs=jobs,
        more_after=more_after,
        proxy=proxy,
    )


def _run(runner: TestRunner, spec: Spec, on_end: OnEnd | None) -> RunSummary:
    """Run the spec; an error that stops the run is masked like every other message.

    ``complete`` is whether every test ran all of its executions. A ceiling passed at the
    last test, or by what the read after it found, left none out: ``stopped`` says it was
    passed (HB-32). So did repeated errors whose last was the last execution of the last
    test; a test they stopped halfway is in the result, and is not complete (HB-34).
    """
    complete = False
    try:
        summary = runner.run(spec)
        every_test = summary.total == sum(len(suite.tests) for suite in spec.suites)
        complete = every_test and not runner.interrupted
        return summary
    except (AgentStartError, AgentNotObservedError) as e:
        if not spec.agent.secrets:
            raise
        raise type(e)(redact(str(e), spec.agent.secrets)) from None
    finally:
        # A run that stopped halfway still has the results of the tests that finished.
        if on_end is not None:
            on_end(runner.snapshot(), complete)


def agent_log_path(spec_path: str) -> Path:
    """``.highbar/logs/<spec stem>-<timestamp>.log``, relative to where highbar runs."""
    return log_path(spec_path)
