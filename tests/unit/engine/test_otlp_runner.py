"""Tests judged from OpenTelemetry spans (HB-48): the trace id is the test's.

The fake agent writes OTLP JSON lines to a real file, read by the real collector, as an
agent whose instrumentation continues the `traceparent` highbar sent would export them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from highbar.adapters.file_collector import FileTraceCollector
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Budget,
    Expect,
    FunctionCallExpect,
    RunSummary,
    Spec,
    Status,
    Suite,
)
from highbar.domain.models import Test as Case
from highbar.engine.placeholders import Values
from highbar.engine.runner import TestRunner as Runner
from tests.otlp_spans import agent_span, line, openinference_tool, tool_span
from tests.unit.engine.fakes import FakeAgent

Spans = Callable[[str], list[dict[str, Any]]]


class _OtlpAgent:
    """Answers "ok" and appends the spans ``spans(trace_id)`` makes, as one export."""

    def __init__(self, path: Path, spans: Spans) -> None:
        self._path = path
        self._spans = spans
        self.trace_ids: list[str] = []

    def send(self, message: str, *, values: Values) -> str:
        self.trace_ids.append(values.test_id)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line(*self._spans(values.test_id)))
        return "ok"


def _run(
    tmp_path: Path,
    spans: Spans,
    expect: Expect,
    *,
    runs: int = 1,
    reporter: MagicMock | None = None,
) -> RunSummary:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "otel.jsonl"
    path.touch()
    return Runner(
        adapter=MagicMock(),
        client=_OtlpAgent(path, spans),  # pyright: ignore[reportArgumentType]
        collector=FileTraceCollector(path, trace_format="otlp"),
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=0,
        managed=False,
        trace_format="otlp",
    ).run(
        Spec(
            agent=AgentConfig(endpoint="http://127.0.0.1:1/chat", traces=str(path)),
            suites=[Suite("S", [Case(trigger="where is A1", expect=expect, runs=runs)])],
        )
    )


GET_ORDER = Expect(function_calls=[FunctionCallExpect(name="get_order", params={"id": "A1"})])


def test_a_function_call_passes_from_an_execute_tool_span(tmp_path: Path) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [tool_span(trace, "get_order", {"id": "A1"}), agent_span(trace)]

    (test,) = _run(tmp_path, spans, GET_ORDER).tests

    assert test.status is Status.PASSED, test.errors


def test_a_function_call_passes_from_an_openinference_tool_span(tmp_path: Path) -> None:
    (test,) = _run(
        tmp_path, lambda trace: [openinference_tool(trace, "get_order", {"id": "A1"})], GET_ORDER
    ).tests

    assert test.status is Status.PASSED, test.errors


def test_wrong_arguments_fail(tmp_path: Path) -> None:
    (test,) = _run(
        tmp_path, lambda trace: [tool_span(trace, "get_order", {"id": "B2"})], GET_ORDER
    ).tests

    assert test.status is Status.FAILED, test.errors


def test_a_span_of_another_trace_does_not_count(tmp_path: Path) -> None:
    other = "0af7651916cd43dd8448eb211c80319c"

    def spans(trace: str) -> list[dict[str, Any]]:
        return [tool_span(other, "get_order", {"id": "A1"}), agent_span(other)]

    (test,) = _run(tmp_path, spans, GET_ORDER).tests

    assert test.status is Status.ERROR, test.errors
    (error,) = test.errors
    assert error.startswith("not observed")
    assert "traceparent" in error


def test_a_guardrail_is_not_observed_without_a_span_of_its_trace(tmp_path: Path) -> None:
    (test,) = _run(tmp_path, lambda trace: [], Expect(function_calls=[])).tests

    assert test.status is Status.ERROR
    assert test.errors[0].startswith("not observed")


def test_params_on_a_span_without_arguments_is_an_error_naming_content_capture(
    tmp_path: Path,
) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [tool_span(trace, "get_order"), agent_span(trace)]

    (test,) = _run(tmp_path, spans, GET_ORDER).tests

    assert test.status is Status.ERROR, test.errors
    (error,) = test.errors
    assert "arguments of 'get_order' were not recorded" in error
    assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in error


def test_a_name_only_expectation_passes_without_arguments(tmp_path: Path) -> None:
    (test,) = _run(
        tmp_path,
        lambda trace: [tool_span(trace, "get_order")],
        Expect(function_calls=[FunctionCallExpect(name="get_order")]),
    ).tests

    assert test.status is Status.PASSED, test.errors


def test_a_span_without_arguments_still_fails_on_what_can_be_seen(tmp_path: Path) -> None:
    expect = Expect(
        function_calls=[
            FunctionCallExpect(name="get_order", params={"id": "A1"}),
            FunctionCallExpect(name="cancel_order"),
        ]
    )

    (test,) = _run(tmp_path, lambda trace: [tool_span(trace, "get_order")], expect).tests

    assert test.status is Status.FAILED, test.errors


def test_a_tool_span_with_error_status_is_a_failed_call(tmp_path: Path) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        status = {"code": 2, "message": "not found"}
        return [tool_span(trace, "get_order", {"id": "A1"}, status=status), agent_span(trace)]

    (test,) = _run(tmp_path, spans, GET_ORDER).tests

    assert test.status is Status.FAILED, test.errors


def test_a_forbidden_tool_span_fails(tmp_path: Path) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [tool_span(trace, "refund", {"id": "A1"}), agent_span(trace)]

    (test,) = _run(tmp_path, spans, Expect(function_calls=[], forbid=("refund",))).tests

    assert test.status is Status.FAILED
    assert any("forbidden tool 'refund' was called" in e for e in test.errors), test.errors


def test_an_invoke_agent_span_gives_the_usage_a_budget_holds_against(tmp_path: Path) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [agent_span(trace, input_tokens=120, output_tokens=30, model="gpt-4o-mini")]

    within = Expect(budget=Budget(max_input_tokens=500))
    over = Expect(budget=Budget(max_input_tokens=100))

    (passed,) = _run(tmp_path / "within", spans, within).tests
    (failed,) = _run(tmp_path / "over", spans, over).tests

    assert passed.status is Status.PASSED, passed.errors
    assert failed.status is Status.FAILED, failed.errors


def test_a_budget_without_usage_in_the_spans_is_an_error(tmp_path: Path) -> None:
    (test,) = _run(
        tmp_path, lambda trace: [agent_span(trace)], Expect(budget=Budget(max_input_tokens=500))
    ).tests

    assert test.status is Status.ERROR, test.errors


def test_each_execution_has_its_own_trace_id(tmp_path: Path) -> None:
    path = tmp_path / "otel.jsonl"
    path.touch()
    agent = _OtlpAgent(path, lambda trace: [agent_span(trace)])
    Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=FileTraceCollector(path, trace_format="otlp"),
        assertions=[FunctionCallsAssertion()],
        reporter=MagicMock(),
        settle_time=0,
        managed=False,
        trace_format="otlp",
    ).run(
        Spec(
            agent=AgentConfig(endpoint="http://127.0.0.1:1/chat", traces=str(path)),
            suites=[Suite("S", [Case(trigger="hi", expect=Expect(function_calls=[]), runs=3)])],
        )
    )

    assert len(set(agent.trace_ids)) == 3


def test_a_late_span_fails_its_own_run(tmp_path: Path) -> None:
    """A forbidden call exported after its test's read still fails that test (unchanged)."""
    path = tmp_path / "otel.jsonl"
    path.touch()
    pending: list[str] = []

    class _Late(_OtlpAgent):
        def send(self, message: str, *, values: Values) -> str:
            with open(self._path, "a", encoding="utf-8") as f:
                # The previous test's forbidden call, exported only now.
                for trace in pending:
                    f.write(line(tool_span(trace, "refund", {})))
                pending.clear()
                f.write(line(agent_span(values.test_id)))
            pending.append(values.test_id)
            return "ok"

    summary = Runner(
        adapter=MagicMock(),
        client=_Late(path, lambda trace: []),  # pyright: ignore[reportArgumentType]
        collector=FileTraceCollector(path, trace_format="otlp"),
        assertions=[FunctionCallsAssertion()],
        reporter=MagicMock(),
        settle_time=0,
        managed=False,
        trace_format="otlp",
    ).run(
        Spec(
            agent=AgentConfig(endpoint="http://127.0.0.1:1/chat", traces=str(path)),
            suites=[
                Suite(
                    "S",
                    [
                        Case(trigger="one", expect=Expect(function_calls=[], forbid=("refund",))),
                        Case(trigger="two", expect=Expect(function_calls=[])),
                    ],
                )
            ],
        )
    )

    first = summary.tests[0]
    assert first.status is Status.FAILED, first.errors


def _warnings(reporter: MagicMock) -> list[str]:
    return [call[0][0] for call in reporter.on_warning.call_args_list]


def test_a_guardrail_warns_that_otlp_declares_no_tools(tmp_path: Path) -> None:
    reporter = MagicMock()

    (test,) = _run(
        tmp_path,
        lambda trace: [agent_span(trace)],
        Expect(function_calls=[], forbid=("refund",)),
        reporter=reporter,
    ).tests

    assert test.status is Status.PASSED, test.errors
    (warning,) = [w for w in _warnings(reporter) if "refund" in w]
    assert "OTLP trace file declares no tools" in warning
    assert "only because no span of these tools was read" in warning
    assert "execute_tool" in warning and "TOOL span" in warning
    assert "hb.declare" not in warning and "Python SDK" not in warning


def test_the_highbar_format_guardrail_warning_is_unchanged() -> None:
    agent = FakeAgent()  # highbar's own contract, a writer that declares nothing
    reporter = MagicMock()

    Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion()],
        reporter=reporter,
        settle_time=0,
    ).run(
        Spec(
            agent=AgentConfig(endpoint="http://127.0.0.1:1/chat"),
            suites=[Suite("S", [Case(trigger="hi", expect=Expect(forbid=("refund",)))])],
        )
    )

    assert [w for w in _warnings(reporter) if "refund" in w] == [
        "the agent has not declared the tools it records, so these guardrails cannot tell a "
        "tool it never records from one it did not call: refund. highbar's Python SDK "
        "declares them after each hello since 0.1.0rc2; another writer adds a `tools` record "
        "(see the trace contract)"
    ]
