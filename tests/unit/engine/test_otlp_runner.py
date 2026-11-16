"""Tests judged from OpenTelemetry spans (HB-48): the trace id is the test's.

The fake agent writes OTLP JSON lines to a real file, read by the real collector, as an
agent whose instrumentation continues the `traceparent` highbar sent would export them.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

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


# agent.traces.tools: what an OTLP agent says its tools are (review of #98).
TOOLS = ("get_order", "cancel_order", "refund")


def _run(
    tmp_path: Path,
    spans: Spans,
    expect: Expect,
    *,
    runs: int = 1,
    reporter: MagicMock | None = None,
    tools: tuple[str, ...] | None = TOOLS,
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
            agent=_agent(path, tools),
            suites=[Suite("S", [Case(trigger="where is A1", expect=expect, runs=runs)])],
        )
    )


def _agent(path: Path, tools: tuple[str, ...] | None = TOOLS) -> AgentConfig:
    return AgentConfig(
        endpoint="http://127.0.0.1:1/chat",
        traces=str(path),
        trace_format="otlp",
        trace_tools=tools,
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
            agent=_agent(path),
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
            agent=_agent(path),
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


# --- review of #98 -------------------------------------------------------------------

FRESH = "0af7651916cd43dd8448eb211c80319c"  # a trace id highbar never sent
NO_REFUND = Expect(forbid=("refund",))


def _stray_refund(trace: str) -> list[dict[str, Any]]:
    """The agent ran `refund` in a worker that started a new root trace: the span lost
    the test's trace id, while the request's own span kept it."""
    return [tool_span(FRESH, "refund", {"id": "A1"}), agent_span(trace)]


def test_f1_a_tool_span_on_a_fresh_trace_errors_a_forbid(tmp_path: Path) -> None:
    (test,) = _run(tmp_path, _stray_refund, NO_REFUND).tests

    assert test.status is Status.ERROR, test.errors
    (error,) = test.errors
    assert "cannot tell which test made refund" in error
    assert "trace id" in error and "traceparent" in error
    assert "hb.record" not in error


def test_f1_a_tool_span_on_a_fresh_trace_errors_no_calls(tmp_path: Path) -> None:
    (test,) = _run(tmp_path, _stray_refund, Expect(function_calls=[])).tests

    assert test.status is Status.ERROR, test.errors


def test_f1_a_fresh_trace_span_that_changes_nothing_leaves_the_test_passed(
    tmp_path: Path,
) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [tool_span(FRESH, "get_order", {"id": "A1"}), agent_span(trace)]

    (test,) = _run(tmp_path, spans, NO_REFUND).tests

    assert test.status is Status.PASSED, test.errors


def test_f1_the_warning_names_the_lost_trace_context(tmp_path: Path) -> None:
    reporter = MagicMock()

    _run(tmp_path, _stray_refund, NO_REFUND, reporter=reporter)

    (warning,) = [w for w in _warnings(reporter) if "count for no test" in w]
    assert "trace id" in warning and "traceparent" in warning
    assert "X-Highbar-Test" not in warning and "highbar.asgi" not in warning


def test_f1_the_warning_gives_the_otlp_fix_not_the_sdk_tag(tmp_path: Path) -> None:
    """Review of #98, note 1: the advice at the end is OpenTelemetry's, not the SDK's."""
    reporter = MagicMock()

    _run(tmp_path, _stray_refund, NO_REFUND, reporter=reporter)

    (warning,) = [w for w in _warnings(reporter) if "count for no test" in w]
    assert "to keep the tag" not in warning
    assert "opentelemetry.context.attach" in warning


def test_f1_the_usage_warning_gives_the_otlp_fix_not_the_sdk_tag(tmp_path: Path) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [agent_span(FRESH, input_tokens=50, output_tokens=5), agent_span(trace)]

    reporter = MagicMock()

    _run(tmp_path, spans, NO_REFUND, reporter=reporter)

    (warning,) = [w for w in _warnings(reporter) if "usage" in w and "no test" in w]
    assert "trace id" in warning
    assert "to keep the tag" not in warning
    assert "opentelemetry.context.attach" in warning


def test_f1_only_spans_of_a_fresh_trace_are_not_observed(tmp_path: Path) -> None:
    (test,) = _run(tmp_path, lambda trace: [tool_span(FRESH, "refund", {})], NO_REFUND).tests

    assert test.status is Status.ERROR, test.errors
    assert test.errors[0].startswith("not observed"), test.errors


def _two_tests(
    path: Path, client: _OtlpAgent, first: Expect, second: Expect, *, jobs: int = 1
) -> RunSummary:
    return Runner(
        adapter=MagicMock(),
        client=client,  # pyright: ignore[reportArgumentType]
        collector=FileTraceCollector(path, trace_format="otlp"),
        assertions=[FunctionCallsAssertion()],
        reporter=MagicMock(),
        settle_time=0,
        managed=False,
        trace_format="otlp",
        jobs=jobs,
    ).run(
        Spec(
            agent=_agent(path),
            suites=[
                Suite("S", [Case(trigger="one", expect=first), Case(trigger="two", expect=second)])
            ],
        )
    )


def test_f1_a_fresh_trace_span_exported_late_errors_the_test_before(tmp_path: Path) -> None:
    path = tmp_path / "otel.jsonl"
    path.touch()

    class _LateStray(_OtlpAgent):
        def send(self, message: str, *, values: Values) -> str:
            with open(self._path, "a", encoding="utf-8") as f:
                if message == "two":
                    # The first test's refund, on a new root trace, exported only now.
                    f.write(line(tool_span(FRESH, "refund", {})))
                f.write(line(agent_span(values.test_id)))
            return "ok"

    first, second = _two_tests(path, _LateStray(path, lambda trace: []), NO_REFUND, Expect()).tests

    assert first.status is Status.ERROR, first.errors
    assert "refund" in first.errors[0]


def test_f1_a_fresh_trace_span_after_the_last_test_errors_it(tmp_path: Path) -> None:
    path = tmp_path / "otel.jsonl"
    path.touch()
    sent: list[str] = []

    class _AfterLast(_OtlpAgent):
        def send(self, message: str, *, values: Values) -> str:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line(agent_span(values.test_id)))
            sent.append(message)
            return "ok"

    class _Collector(FileTraceCollector):
        def collect(self, since: int) -> Any:
            if len(sent) == 2 and not getattr(self, "_wrote", False):
                self._wrote = True
                # After the last test's window: the read after it is the next one.
                window = super().collect(since)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line(tool_span(FRESH, "refund", {})))
                return window
            return super().collect(since)

    summary = Runner(
        adapter=MagicMock(),
        client=_AfterLast(path, lambda trace: []),  # pyright: ignore[reportArgumentType]
        collector=_Collector(path, trace_format="otlp"),
        assertions=[FunctionCallsAssertion()],
        reporter=MagicMock(),
        settle_time=0,
        managed=False,
        trace_format="otlp",
    ).run(
        Spec(
            agent=_agent(path),
            suites=[
                Suite(
                    "S",
                    [Case(trigger="one", expect=NO_REFUND), Case(trigger="two", expect=NO_REFUND)],
                )
            ],
        )
    )

    assert summary.tests[1].status is Status.ERROR, summary.tests[1].errors


def test_f1_with_jobs_a_fresh_trace_span_errors_every_test_it_could_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "otel.jsonl"
    path.touch()

    summary = _two_tests(path, _OtlpAgent(path, _stray_refund), NO_REFUND, NO_REFUND, jobs=2)

    assert [t.status for t in summary.tests] == [Status.ERROR, Status.ERROR], [
        t.errors for t in summary.tests
    ]


def test_tools_a_forbid_of_a_name_not_listed_errors_with_a_suggestion(tmp_path: Path) -> None:
    (test,) = _run(tmp_path, lambda trace: [agent_span(trace)], Expect(forbid=("refnd",))).tests

    assert test.status is Status.ERROR, test.errors
    (error,) = test.errors
    assert "'refnd' (did you mean 'refund'?)" in error
    assert "agent.traces.tools" in error
    assert "hb.declare" not in error


def test_tools_a_listed_forbid_not_called_passes(tmp_path: Path) -> None:
    (test,) = _run(tmp_path, lambda trace: [agent_span(trace)], NO_REFUND).tests

    assert test.status is Status.PASSED, test.errors


@pytest.mark.parametrize(
    "expect",
    [
        NO_REFUND,
        Expect(function_calls=[]),
        Expect(function_calls=[FunctionCallExpect(name="get_order")], exclusive=True),
    ],
    ids=["forbid", "no_calls", "exclusive"],
)
def test_tools_without_a_list_a_test_that_rests_on_absence_is_an_error(
    tmp_path: Path, expect: Expect
) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [tool_span(trace, "get_order", {"id": "A1"}), agent_span(trace)]

    if expect.function_calls == []:
        spans = lambda trace: [agent_span(trace)]  # noqa: E731

    (test,) = _run(tmp_path, spans, expect, tools=None).tests

    assert test.status is Status.ERROR, test.errors
    (error,) = test.errors
    assert "agent.traces.tools" in error


def test_tools_without_a_list_a_forbidden_span_still_fails(tmp_path: Path) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [tool_span(trace, "refund", {"id": "A1"}), agent_span(trace)]

    (test,) = _run(tmp_path, spans, NO_REFUND, tools=None).tests

    assert test.status is Status.FAILED, test.errors
    assert "forbidden tool 'refund' was called" in test.errors[0]


def test_tools_without_a_list_a_test_of_present_calls_still_passes(tmp_path: Path) -> None:
    def spans(trace: str) -> list[dict[str, Any]]:
        return [tool_span(trace, "get_order", {"id": "A1"}), agent_span(trace)]

    (test,) = _run(tmp_path, spans, GET_ORDER, tools=None).tests

    assert test.status is Status.PASSED, test.errors


def _untimed(trace: str, name: str) -> dict[str, Any]:
    record = tool_span(trace, name, {})
    del record["startTimeUnixNano"], record["endTimeUnixNano"]
    return record


def test_f2_ordered_on_spans_without_timestamps_is_an_error(tmp_path: Path) -> None:
    expect = Expect(
        function_calls=[FunctionCallExpect(name="get_order"), FunctionCallExpect(name="refund")],
        ordered=True,
    )

    def spans(trace: str) -> list[dict[str, Any]]:
        # Written in the wrong order: nothing says which ran first.
        return [_untimed(trace, "refund"), _untimed(trace, "get_order"), agent_span(trace)]

    (test,) = _run(tmp_path, spans, expect).tests

    assert test.status is Status.ERROR, test.errors
    (error,) = test.errors
    assert "start and end times of 'get_order' and 'refund' were not recorded" in error
    assert "`ordered`" in error


def test_f2_ordered_still_fails_on_timed_spans_out_of_order(tmp_path: Path) -> None:
    expect = Expect(
        function_calls=[FunctionCallExpect(name="get_order"), FunctionCallExpect(name="refund")],
        ordered=True,
    )

    def spans(trace: str) -> list[dict[str, Any]]:
        return [
            tool_span(trace, "refund", {}, start=1, end=2),
            tool_span(trace, "get_order", {}, start=3, end=4),
            agent_span(trace),
        ]

    (test,) = _run(tmp_path, spans, expect).tests

    assert test.status is Status.FAILED, test.errors


def test_f2_untimed_spans_without_ordered_pass(tmp_path: Path) -> None:
    expect = Expect(
        function_calls=[FunctionCallExpect(name="get_order"), FunctionCallExpect(name="refund")]
    )

    def spans(trace: str) -> list[dict[str, Any]]:
        return [_untimed(trace, "refund"), _untimed(trace, "get_order"), agent_span(trace)]

    (test,) = _run(tmp_path, spans, expect).tests

    assert test.status is Status.PASSED, test.errors
