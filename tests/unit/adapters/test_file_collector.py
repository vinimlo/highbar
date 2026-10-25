"""Tests for the trace collector: contract v1, v0 lines, and reading by byte offset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highbar.adapters.file_collector import FileTraceCollector, TraceFormatError
from highbar.domain.models import Hello, Trace, TurnRecord, Window


def _line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False) + "\n"


def _tool(fn: str = "get_order", **fields: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "type": "tool",
        "ts": 1.0,
        "fn": fn,
        "args": {},
        "result": None,
        "started_at": 1.0,
        "ended_at": 1.2,
        **fields,
    }


def _write(path: Path, *lines: str) -> FileTraceCollector:
    path.write_text("".join(lines), encoding="utf-8")
    return FileTraceCollector(path)


# The exact shape of a line written today by the Seu AgenteIA emitter (lib/gauntlet_trace.py).
V0_LINE = {
    "id": "ab12",
    "fn": "search_products",
    "args": {"query": "coca"},
    "result": {"items": []},
    "started_at": 1.0,
    "ended_at": 2.0,
    "conversation_id": 42,
    "person_id": None,
    "seq": 0,
}


class TestContractV1:
    def test_v1_tool_record_is_read(self, tmp_path: Path) -> None:
        collector = _write(
            tmp_path / "t.jsonl",
            _line(_tool(args={"id": 7}, result={"ok": True}, context={"test": "t1"})),
        )

        window = collector.collect(since=0)

        assert window.tools == [
            Trace(
                fn="get_order",
                args={"id": 7},
                result={"ok": True},
                started_at=1.0,
                ended_at=1.2,
                context={"test": "t1"},
            )
        ]

    def test_error_field_is_the_only_failure_signal(self, tmp_path: Path) -> None:
        """A tool may legitimately return an object with an `error` key."""
        collector = _write(
            tmp_path / "t.jsonl",
            _line(_tool(result={"error": "not found"})),
            _line(_tool(error="boom")),
        )

        first, second = collector.collect(since=0).tools

        assert first.error is None
        assert second.error == "boom"

    def test_turn_and_hello_records_are_read(self, tmp_path: Path) -> None:
        collector = _write(
            tmp_path / "t.jsonl",
            _line({"v": 1, "type": "hello", "ts": 1.0, "sdk": "highbar-python/0.1", "pid": 9}),
            _line(
                {
                    "v": 1,
                    "type": "turn",
                    "ts": 2.0,
                    "context": {"test": "t1"},
                    "message": "hi",
                    "response": "hello",
                }
            ),
        )

        window = collector.collect(since=0)

        assert window.hellos == [Hello(ts=1.0, sdk="highbar-python/0.1", pid=9)]
        assert window.turns == [
            TurnRecord(ts=2.0, context={"test": "t1"}, message="hi", response="hello")
        ]

    def test_unknown_types_and_fields_are_ignored(self, tmp_path: Path) -> None:
        collector = _write(
            tmp_path / "t.jsonl",
            _line({"v": 1, "type": "span", "ts": 1.0, "name": "llm"}),
            _line(_tool(model="gpt")),
        )

        window = collector.collect(since=0)

        assert window.turns == [] and window.hellos == []
        assert [t.fn for t in window.tools] == ["get_order"]

    def test_v0_line_is_a_tool_with_extra_fields_as_context(self, tmp_path: Path) -> None:
        collector = _write(tmp_path / "t.jsonl", _line(V0_LINE))

        (trace,) = collector.collect(since=0).tools

        assert trace.fn == "search_products"
        assert trace.args == {"query": "coca"}
        assert trace.result == {"items": []}
        assert trace.error is None
        assert trace.context == {"conversation_id": 42, "person_id": None}

    def test_utf8_survives(self, tmp_path: Path) -> None:
        collector = _write(tmp_path / "t.jsonl", _line(_tool(args={"produto": "pão de queijo"})))

        (trace,) = collector.collect(since=0).tools

        assert trace.args == {"produto": "pão de queijo"}


class TestBrokenLines:
    """A line that cannot be read is an error with its line number, never skipped."""

    def test_invalid_json_line_is_an_error_with_its_line_number(self, tmp_path: Path) -> None:
        collector = _write(tmp_path / "t.jsonl", _line(_tool()), "not json\n")

        with pytest.raises(TraceFormatError, match="line 2: not valid JSON"):
            collector.collect(since=0)

    def test_line_number_counts_from_the_start_of_the_file(self, tmp_path: Path) -> None:
        collector = _write(tmp_path / "t.jsonl", _line(_tool()), _line(_tool()), "{oops\n")
        since = len(_line(_tool()).encode())

        with pytest.raises(TraceFormatError, match="line 3: "):
            collector.collect(since=since)

    def test_newer_format_version_is_an_error(self, tmp_path: Path) -> None:
        collector = _write(tmp_path / "t.jsonl", _line({**_tool(), "v": 2}))

        with pytest.raises(TraceFormatError, match=r"line 1: trace format v2 .*reads v1"):
            collector.collect(since=0)

    def test_tool_record_without_fn_is_an_error(self, tmp_path: Path) -> None:
        record = _tool()
        del record["fn"]
        collector = _write(tmp_path / "t.jsonl", _line(record))

        with pytest.raises(TraceFormatError, match="line 1: tool record without 'fn'"):
            collector.collect(since=0)

    def test_args_must_be_an_object(self, tmp_path: Path) -> None:
        collector = _write(tmp_path / "t.jsonl", _line(_tool(args=["x"])))

        with pytest.raises(TraceFormatError, match="line 1: 'args' must be an object"):
            collector.collect(since=0)

    def test_message_names_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        collector = _write(path, "nope\n")

        with pytest.raises(TraceFormatError, match=str(path)):
            collector.collect(since=0)


class TestOffsets:
    def test_missing_file_is_an_empty_window_and_checkpoint_0(self, tmp_path: Path) -> None:
        collector = FileTraceCollector(tmp_path / "none.jsonl")

        assert collector.checkpoint() == 0
        assert collector.collect(since=0) == Window()

    def test_checkpoint_is_the_byte_offset_after_the_last_complete_line(
        self, tmp_path: Path
    ) -> None:
        first = _line(_tool(args={"produto": "pão"}))
        collector = _write(tmp_path / "t.jsonl", first, '{"v": 1')

        assert collector.checkpoint() == len(first.encode())

    def test_collect_reads_only_what_came_after_the_checkpoint(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        collector = _write(path, _line(_tool("old")))
        since = collector.checkpoint()
        with path.open("a", encoding="utf-8") as f:
            f.write(_line(_tool("new")))

        assert [t.fn for t in collector.collect(since=since).tools] == ["new"]

    def test_partial_last_line_is_left_for_the_next_collect(self, tmp_path: Path) -> None:
        """The agent may be mid-write: half a line is neither an error nor lost."""
        path = tmp_path / "t.jsonl"
        whole = _line(_tool("second"))
        collector = _write(path, _line(_tool("first")), whole[:10])

        assert [t.fn for t in collector.collect(since=0).tools] == ["first"]
        since = collector.checkpoint()

        with path.open("a", encoding="utf-8") as f:
            f.write(whole[10:])

        assert [t.fn for t in collector.collect(since=since).tools] == ["second"]

    def test_truncated_file_is_read_from_the_start(self, tmp_path: Path) -> None:
        """A recreated file (restarted container, rotation) is smaller than the offset."""
        collector = _write(tmp_path / "t.jsonl", _line(_tool("after-restart")))

        assert [t.fn for t in collector.collect(since=10_000).tools] == ["after-restart"]
