"""OTLP JSON lines read as trace records (HB-48): the mapping, versioned in code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from highbar.adapters.file_collector import FileTraceCollector, TraceFormatError, trace_collector
from highbar.adapters.otlp import OTLP_MAPPING_VERSION
from tests.otlp_spans import agent_span, line, openinference_tool, span, tool_span

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
OTHER = "0af7651916cd43dd8448eb211c80319c"


def _otlp(path: Path, *lines: str) -> FileTraceCollector:
    path.write_text("".join(lines), encoding="utf-8")
    return FileTraceCollector(path, trace_format="otlp")


def test_the_mapping_is_versioned() -> None:
    assert OTLP_MAPPING_VERSION == 0


def test_an_execute_tool_span_is_a_tool_record_of_its_trace(tmp_path: Path) -> None:
    collector = _otlp(
        tmp_path / "otel.jsonl",
        line(tool_span(TRACE, "get_order", {"id": "A1"}, {"status": "shipped"}, start=2, end=3)),
    )

    (tool,) = collector.collect(since=0).tools

    assert tool.fn == "get_order"
    assert tool.args == {"id": "A1"}
    assert tool.result == {"status": "shipped"}
    assert tool.has_args and tool.has_result
    assert (tool.started_at, tool.ended_at) == (2.0, 3.0)
    assert tool.error is None
    assert tool.context == {"test": TRACE}


def test_an_openinference_tool_span_is_a_tool_record(tmp_path: Path) -> None:
    collector = _otlp(
        tmp_path / "otel.jsonl", line(openinference_tool(TRACE, "search", {"q": "x"}))
    )

    (tool,) = collector.collect(since=0).tools

    assert (tool.fn, tool.args, tool.context) == ("search", {"q": "x"}, {"test": TRACE})
    assert not tool.has_result


def test_a_tool_span_without_arguments_says_they_were_not_captured(tmp_path: Path) -> None:
    collector = _otlp(tmp_path / "otel.jsonl", line(tool_span(TRACE, "search")))

    (tool,) = collector.collect(since=0).tools

    assert tool.args == {}
    assert not tool.has_args
    assert not tool.has_result


def test_an_error_status_is_the_calls_error(tmp_path: Path) -> None:
    failed = tool_span(TRACE, "pay", {}, status={"code": 2, "message": "card declined"})
    typed = span(
        TRACE,
        "execute_tool refund",
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "refund",
            "error.type": "TimeoutError",
        },
    )
    collector = _otlp(tmp_path / "otel.jsonl", line(failed, typed))

    pay, refund = collector.collect(since=0).tools

    assert pay.error == "card declined"
    assert refund.error == "TimeoutError"


def test_an_invoke_agent_span_is_a_turn_with_usage(tmp_path: Path) -> None:
    collector = _otlp(
        tmp_path / "otel.jsonl",
        line(
            agent_span(
                TRACE,
                input_tokens=120,
                output_tokens=30,
                model="gpt-4o-mini",
                response="Your order shipped.",
                end=4.0,
            )
        ),
    )

    (turn,) = collector.collect(since=0).turns

    assert turn.context == {"test": TRACE}
    assert turn.ts == 4.0
    assert turn.response == "Your order shipped."
    assert turn.usage is not None
    assert (turn.usage.input_tokens, turn.usage.output_tokens) == (120, 30)
    assert turn.usage.model == "gpt-4o-mini"


def test_an_openinference_agent_span_is_a_turn(tmp_path: Path) -> None:
    agent = span(
        TRACE, "agent", {"openinference.span.kind": "AGENT", "output.value": "done"}, end=2.0
    )
    collector = _otlp(tmp_path / "otel.jsonl", line(agent))

    (turn,) = collector.collect(since=0).turns

    assert (turn.response, turn.context) == ("done", {"test": TRACE})


def test_chat_and_retriever_spans_are_left_for_v1(tmp_path: Path) -> None:
    chat = span(TRACE, "chat gpt-4o", {"gen_ai.operation.name": "chat"})
    retriever = span(TRACE, "retrieve", {"openinference.span.kind": "RETRIEVER"})
    collector = _otlp(tmp_path / "otel.jsonl", line(chat, retriever))

    window = collector.collect(since=0)

    assert (window.tools, window.turns) == ([], [])


def test_spans_keep_their_own_trace_id(tmp_path: Path) -> None:
    collector = _otlp(
        tmp_path / "otel.jsonl",
        line(tool_span(TRACE, "a", {})),
        line(tool_span(OTHER.upper(), "b", {})),
    )

    tools = collector.collect(since=0).tools

    assert [tool.context for tool in tools] == [{"test": TRACE}, {"test": OTHER}]


def test_a_malformed_line_is_an_unreadable_line(tmp_path: Path) -> None:
    collector = _otlp(
        tmp_path / "otel.jsonl",
        line(tool_span(TRACE, "a", {})),
        '{"resourceSpans": [\n',
        line(tool_span(TRACE, "b", {})),
    )

    with pytest.raises(TraceFormatError, match=r"otel\.jsonl line 2: not valid JSON") as caught:
        collector.collect(since=0)

    assert caught.value.window is not None
    assert [tool.fn for tool in caught.value.window.tools] == ["a", "b"]


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        ({"resourceSpans": {}}, "resourceSpans must be a list"),
        (
            json.loads(line({**tool_span(TRACE, "x", {}), "traceId": "not-hex"})),
            "span without a valid traceId",
        ),
        (
            json.loads(
                line(
                    {
                        **tool_span(TRACE, "a", {}),
                        "attributes": [
                            {
                                "key": "gen_ai.operation.name",
                                "value": {"stringValue": "execute_tool"},
                            }
                        ],
                        "name": "execute_tool",
                    }
                )
            ),
            "execute_tool span without gen_ai.tool.name",
        ),
        (
            json.loads(
                line(
                    {
                        **tool_span(TRACE, "a"),
                        "attributes": [
                            {
                                "key": "gen_ai.operation.name",
                                "value": {"stringValue": "execute_tool"},
                            },
                            {"key": "gen_ai.tool.name", "value": {"stringValue": "a"}},
                            {"key": "gen_ai.tool.call.arguments", "value": {"stringValue": "[1]"}},
                        ],
                    }
                )
            ),
            "arguments of 'a' must be a JSON object, got list",
        ),
    ],
)
def test_a_span_that_cannot_be_read_names_its_line(
    tmp_path: Path, bad: dict[str, object], problem: str
) -> None:
    collector = _otlp(tmp_path / "otel.jsonl", json.dumps(bad) + "\n")

    with pytest.raises(TraceFormatError, match=rf"line 1: .*{problem}"):
        collector.collect(since=0)


def test_other_signals_in_the_file_are_ignored(tmp_path: Path) -> None:
    collector = _otlp(
        tmp_path / "otel.jsonl",
        json.dumps({"resourceLogs": []}) + "\n",
        line(tool_span(TRACE, "a", {})),
    )

    assert [tool.fn for tool in collector.collect(since=0).tools] == ["a"]


def test_reading_by_offset_reads_only_new_lines(tmp_path: Path) -> None:
    path = tmp_path / "otel.jsonl"
    collector = _otlp(path, line(tool_span(TRACE, "a", {})))
    mark = collector.checkpoint()
    with open(path, "a", encoding="utf-8") as f:
        f.write(line(tool_span(TRACE, "b", {})))

    assert [tool.fn for tool in collector.collect(since=mark).tools] == ["b"]


def test_an_otlp_file_declares_no_tools(tmp_path: Path) -> None:
    assert _otlp(tmp_path / "otel.jsonl", line(tool_span(TRACE, "a", {}))).declared() is None


def test_a_glob_reads_otlp_files(tmp_path: Path) -> None:
    (tmp_path / "a.jsonl").write_text(line(tool_span(TRACE, "a", {})), encoding="utf-8")
    (tmp_path / "b.jsonl").write_text(line(tool_span(TRACE, "b", {})), encoding="utf-8")

    collector = trace_collector(str(tmp_path / "*.jsonl"), trace_format="otlp")

    assert sorted(tool.fn for tool in collector.collect(since=0).tools) == ["a", "b"]


def test_the_default_format_is_highbars_own(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text(line(tool_span(TRACE, "a", {})), encoding="utf-8")

    # Read as highbar's contract, an OTLP line is a v0 line without 'fn'.
    with pytest.raises(TraceFormatError, match="tool record without 'fn'"):
        FileTraceCollector(path).collect(since=0)


def test_f2_a_span_without_timestamps_keeps_its_time_unknown(tmp_path: Path) -> None:
    """Not 0.0: a made-up time would let `ordered` pass on it (review of #98, F2)."""
    untimed = tool_span(TRACE, "a", {})
    del untimed["startTimeUnixNano"], untimed["endTimeUnixNano"]
    only_start = tool_span(TRACE, "b", {}, start=2)
    del only_start["endTimeUnixNano"]

    first, second = _otlp(tmp_path / "otel.jsonl", line(untimed, only_start)).collect(since=0).tools

    assert (first.started_at, first.ended_at) == (None, None)
    assert first.duration_ms is None
    assert (second.started_at, second.ended_at) == (2.0, None)
