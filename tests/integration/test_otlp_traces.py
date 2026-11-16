"""OpenTelemetry traces end to end (HB-48): the real CLI against an agent that exports
OTLP JSON lines, in attach mode, as the Collector's `file` exporter would write them.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from highbar.adapters.ports import accepts_connections

AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "otlp_agent.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _OtlpAgent:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = _free_port()
        self.traces = root / "var" / "otel.jsonl"
        self.traces.parent.mkdir()
        self.traces.touch()
        self.seen = root / "seen.txt"
        self.server: subprocess.Popen[bytes] | None = None

    def start(self, *args: str) -> None:
        self.server = subprocess.Popen(
            [
                sys.executable,
                str(AGENT),
                "--port",
                str(self.port),
                "--traces",
                str(self.traces),
                "--seen",
                str(self.seen),
                *args,
            ]
        )
        deadline = time.monotonic() + 10
        while not accepts_connections("127.0.0.1", self.port):
            assert time.monotonic() < deadline, "the OTLP agent did not come up"
            time.sleep(0.05)

    def stop(self) -> None:
        if self.server is not None:
            self.server.kill()
            self.server.wait()

    def run(
        self, tests: str, tools: str | None = "[get_order, refund]"
    ) -> subprocess.CompletedProcess[str]:
        listed = f"\n    tools: {tools}" if tools is not None else ""
        (self.root / "otel.highbar.yaml").write_text(
            f"""\
agent:
  endpoint: "http://127.0.0.1:{self.port}/chat"
  traces:
    path: var/otel.jsonl
    format: otlp{listed}
  settle_time: 50
suites:
  - name: Orders
    tests:
{tests}""",
            encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, "-m", "highbar.cli.main", "run", "otel.highbar.yaml"],
            cwd=self.root,
            env={**os.environ, "COLUMNS": "200"},
            capture_output=True,
            text=True,
            timeout=30,
        )


@pytest.fixture
def otlp_agent(tmp_path: Path) -> Iterator[_OtlpAgent]:
    agent = _OtlpAgent(tmp_path)
    try:
        yield agent
    finally:
        agent.stop()


PASSING = """\
      - name: looks up the order
        trigger: "where is order A1?"
        runs: 2
        expect:
          function_calls:
            - name: get_order
              params: {id: A1}
          forbid: [refund]
          response: {contains: "A1"}
          budget: {max_input_tokens: 500}
"""


@pytest.mark.timeout(40)
def test_a_spec_passes_from_otlp_spans(otlp_agent: _OtlpAgent) -> None:
    otlp_agent.start()

    result = otlp_agent.run(PASSING)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    received = otlp_agent.seen.read_text().split()
    assert len(received) == 2
    assert len({header.split("-")[1] for header in received}) == 2, received


@pytest.mark.timeout(40)
def test_a_forbidden_tool_span_fails(otlp_agent: _OtlpAgent) -> None:
    otlp_agent.start()

    result = otlp_agent.run(
        """\
      - name: never refunds
        trigger: "please refund A1"
        expect:
          forbid: [refund]
"""
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "forbidden tool 'refund' was called" in result.stdout


@pytest.mark.timeout(40)
def test_an_agent_that_drops_the_traceparent_is_not_observed(otlp_agent: _OtlpAgent) -> None:
    otlp_agent.start("--ignore-traceparent")

    result = otlp_agent.run(PASSING)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "not observed" in result.stdout
    assert "1 passed" not in result.stdout


@pytest.mark.timeout(40)
def test_params_without_captured_content_is_an_error(otlp_agent: _OtlpAgent) -> None:
    otlp_agent.start("--no-content")

    result = otlp_agent.run(PASSING)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in result.stdout


NEVER_REFUNDS = """\
      - name: never refunds
        trigger: "please refund A1"
        expect:
          forbid: [refund]
"""


@pytest.mark.timeout(40)
def test_f1_a_tool_span_on_a_new_root_trace_is_an_error_not_a_pass(
    otlp_agent: _OtlpAgent,
) -> None:
    """The reviewer's case: the tool ran in a worker that started its own trace."""
    otlp_agent.start("--lost-tool-context")

    result = otlp_agent.run(NEVER_REFUNDS)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot tell which test made refund" in result.stdout
    assert "1 passed" not in result.stdout


@pytest.mark.timeout(40)
def test_tools_a_forbid_typo_is_an_error_with_a_suggestion(otlp_agent: _OtlpAgent) -> None:
    otlp_agent.start()

    result = otlp_agent.run(NEVER_REFUNDS.replace("[refund]", "[refnd]"))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "did you mean 'refund'?" in result.stdout


@pytest.mark.timeout(40)
def test_tools_without_the_list_a_guardrail_is_an_error(otlp_agent: _OtlpAgent) -> None:
    otlp_agent.start()

    result = otlp_agent.run(PASSING.replace("please refund", "where is"), tools=None)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "agent.traces.tools" in result.stdout
    assert "1 passed" not in result.stdout
