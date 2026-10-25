"""Run one parsed spec against its agent: shared by the CLI and ``highbar.run()``."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from highbar.adapters.file_collector import FileTraceCollector
from highbar.adapters.http_client import HTTPClient
from highbar.adapters.subprocess_adapter import SubprocessAdapter
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import RunSummary, Spec
from highbar.engine.runner import TestRunner
from highbar.reporters.terminal import TerminalReporter
from highbar.variations.generator import VariationGenerator


def run_spec(
    path: str,
    spec: Spec,
    *,
    generator: VariationGenerator,
    keep_traces: bool,
    console: Console,
) -> RunSummary:
    """Run one spec against its own agent; the trace file is removed unless kept."""
    console.print(f"  Running {escape(path)}")

    with tempfile.NamedTemporaryFile(
        suffix=".jsonl", prefix="highbar-traces-", delete=False
    ) as traces_fd:
        traces_file = traces_fd.name

    adapter = SubprocessAdapter(
        start_cmd=spec.agent.start,
        endpoint=spec.agent.endpoint,
        log_path=agent_log_path(path),
        health_url=spec.agent.health,
        startup_timeout=spec.agent.startup_timeout,
        env={"HIGHBAR_TRACES_URL": traces_file},
    )
    runner = TestRunner(
        adapter=adapter,
        client=HTTPClient(endpoint=spec.agent.endpoint),
        collector=FileTraceCollector(traces_file),
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=TerminalReporter(),
        settle_time=spec.agent.settle_time / 1000.0,
        variations=generator,
    )

    try:
        summary = runner.run(spec)
    finally:
        if keep_traces:
            console.print(f"  Traces kept at {escape(traces_file)}")
        else:
            Path(traces_file).unlink(missing_ok=True)

    if summary.failed or summary.errored:
        # What the agent printed while failing is usually the explanation.
        console.print(f"  Agent log: {escape(str(adapter.log_path))}\n", soft_wrap=True)
    return summary


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
