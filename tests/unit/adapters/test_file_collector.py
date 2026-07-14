"""Tests for file-based trace collector."""

import json
from pathlib import Path

from gauntlet.adapters.file_collector import FileTraceCollector


def _write_traces(path: Path, traces: list[dict]) -> None:
    with open(path, "w") as f:
        for t in traces:
            f.write(json.dumps(t) + "\n")


class TestFileTraceCollector:
    def test_checkpoint_empty_file(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        trace_file.write_text("")
        collector = FileTraceCollector(str(trace_file))
        assert collector.checkpoint() == 0

    def test_checkpoint_nonexistent_file(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        collector = FileTraceCollector(str(trace_file))
        assert collector.checkpoint() == 0

    def test_checkpoint_returns_line_count(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        _write_traces(
            trace_file,
            [
                {
                    "id": "a",
                    "seq": 0,
                    "fn": "f1",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
                {
                    "id": "b",
                    "seq": 1,
                    "fn": "f2",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
                {
                    "id": "c",
                    "seq": 2,
                    "fn": "f3",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
            ],
        )
        collector = FileTraceCollector(str(trace_file))
        assert collector.checkpoint() == 3

    def test_collect_all(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        _write_traces(
            trace_file,
            [
                {
                    "id": "a",
                    "seq": 0,
                    "fn": "f1",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
                {
                    "id": "b",
                    "seq": 1,
                    "fn": "f2",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
            ],
        )
        collector = FileTraceCollector(str(trace_file))
        traces = collector.collect(since=0)
        assert len(traces) == 2
        assert traces[0].fn == "f1"
        assert traces[1].fn == "f2"

    def test_collect_since_checkpoint(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        _write_traces(
            trace_file,
            [
                {
                    "id": "a",
                    "seq": 0,
                    "fn": "old",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
                {
                    "id": "b",
                    "seq": 1,
                    "fn": "new1",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
                {
                    "id": "c",
                    "seq": 2,
                    "fn": "new2",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
            ],
        )
        collector = FileTraceCollector(str(trace_file))
        traces = collector.collect(since=1)
        assert len(traces) == 2
        assert traces[0].fn == "new1"
        assert traces[1].fn == "new2"

    def test_collect_empty_since_high_checkpoint(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        _write_traces(
            trace_file,
            [
                {
                    "id": "a",
                    "seq": 0,
                    "fn": "f1",
                    "args": {},
                    "result": None,
                    "started_at": 1.0,
                    "ended_at": 1.1,
                },
            ],
        )
        collector = FileTraceCollector(str(trace_file))
        traces = collector.collect(since=5)
        assert len(traces) == 0
