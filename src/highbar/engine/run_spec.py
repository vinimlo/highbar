"""Run one parsed spec against its agent: shared by the CLI and ``highbar.run()``."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from highbar.adapters.attached import AttachedAgent
from highbar.adapters.file_collector import FileTraceCollector, NoTraceCollector
from highbar.adapters.http_client import HTTPClient
from highbar.adapters.subprocess_adapter import AgentStartError, SubprocessAdapter
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import RunSummary, Spec
from highbar.domain.protocols import TraceCollector
from highbar.engine.observation import AgentNotObservedError
from highbar.engine.runner import TestRunner
from highbar.engine.secrets import redact
from highbar.engine.session import SessionHooks
from highbar.reporters.terminal import TerminalReporter
from highbar.variations.generator import VariationGenerator


def load_session_hooks(path: str, spec: Spec) -> SessionHooks | None:
    """The spec's session hooks, with the Python ones imported. Raises ``SessionError``."""
    if spec.agent.session is None:
        return None
    hooks = SessionHooks(spec.agent.session, spec_path=path, timeout_ms=spec.agent.timeout)
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
) -> RunSummary:
    """Run one spec against its agent.

    Managed mode (``start``): highbar starts the agent with a new trace file in
    ``HIGHBAR_TRACES_URL``, stops it after, and removes the file unless kept.
    Attach mode: the agent is already running and writes ``traces`` itself; highbar
    neither stops it nor touches its file.
    """
    console.print(f"  Running {escape(path)}")
    agent = spec.agent
    # The CLI's --runs, when given, takes the place of the spec's defaults.runs.
    runs = spec.default_runs if default_runs is None else default_runs
    client = HTTPClient(
        agent.endpoint,
        request=agent.request,
        response_path=agent.response,
        timeout_ms=agent.timeout,
    )

    if agent.start is None:
        collector: TraceCollector = (
            FileTraceCollector(agent.traces) if agent.traces else NoTraceCollector()
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
        )
        return _run(runner, spec)

    with tempfile.NamedTemporaryFile(
        suffix=".jsonl", prefix="highbar-traces-", delete=False
    ) as traces_fd:
        traces_file = traces_fd.name

    adapter = SubprocessAdapter(
        start_cmd=agent.start,
        endpoint=agent.endpoint,
        log_path=agent_log_path(path),
        health_url=agent.health,
        startup_timeout=agent.startup_timeout,
        env={"HIGHBAR_TRACES_URL": traces_file},
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
    )

    try:
        summary = _run(runner, spec)
    finally:
        if keep_traces:
            console.print(f"  Traces kept at {escape(traces_file)}")
        else:
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
) -> TestRunner:
    return TestRunner(
        adapter=adapter,  # type: ignore[arg-type]
        client=client,
        collector=collector,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=TerminalReporter(),
        settle_time=spec.agent.settle_time / 1000.0,
        variations=generator,
        managed=managed,
        session=session,
        spec_path=path,
        correlate=spec.agent.correlate,
        secrets=spec.agent.secrets,
        default_runs=default_runs,
    )


def _run(runner: TestRunner, spec: Spec) -> RunSummary:
    """Run the spec; an error that stops the run is masked like every other message."""
    try:
        return runner.run(spec)
    except (AgentStartError, AgentNotObservedError) as e:
        if not spec.agent.secrets:
            raise
        raise type(e)(redact(str(e), spec.agent.secrets)) from None


def agent_log_path(spec_path: str) -> Path:
    """``.highbar/logs/<spec stem>-<timestamp>.log``, relative to where highbar runs.

    Never an existing file: two specs with the same name in the same second would
    share a log, and one's failure would show the other agent's output.
    """
    stem = Path(spec_path).name.removesuffix(".yaml").removesuffix(".highbar")
    base = Path(".highbar") / "logs" / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}"
    path, n = base.with_suffix(".log"), 1
    while path.exists():
        n += 1
        path = base.parent / f"{base.name}-{n}.log"
    return path
