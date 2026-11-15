"""HB-15 v1: one trace file per process, read together through a glob in agent.traces.

An agent with several processes (an HTTP server and queue workers) writes one file per
process, so no two of them share a file that a bind mount may not append to atomically.
highbar reads every file the glob matches as one stream, in time order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from highbar.adapters.file_collector import (
    FileTraceCollector,
    GlobTraceCollector,
    TraceFormatError,
    trace_collector,
)


def _line(record: dict[str, Any]) -> str:
    return json.dumps(record) + "\n"


def _tool(fn: str, ended_at: float) -> str:
    return _line(
        {
            "v": 1,
            "type": "tool",
            "fn": fn,
            "args": {},
            "result": None,
            "started_at": ended_at - 0.1,
            "ended_at": ended_at,
            "context": {"test": "t1"},
        }
    )


def _hello(*tools: str, pid: int = 1) -> str:
    return _line({"v": 1, "type": "hello", "ts": 1.0, "pid": pid, "tools": list(tools)})


def _bye(pid: int) -> str:
    return _line({"v": 1, "type": "bye", "ts": 9.0, "pid": pid})


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "traces"
    path.mkdir()
    return path


def _glob(folder: Path) -> GlobTraceCollector:
    return GlobTraceCollector(str(folder / "*.jsonl"))


class TestReadTogether:
    def test_hb15_the_files_a_glob_matches_are_read_in_time_order(self, folder: Path) -> None:
        (folder / "traces-101.jsonl").write_text(_tool("a", 1.0) + _tool("c", 3.0))
        (folder / "traces-202.jsonl").write_text(_tool("b", 2.0))

        window = _glob(folder).collect(since=0)

        assert [tool.fn for tool in window.tools] == ["a", "b", "c"]

    def test_hb15_a_checkpoint_holds_a_place_in_every_file(self, folder: Path) -> None:
        server, worker = folder / "server.jsonl", folder / "worker.jsonl"
        server.write_text(_tool("old-1", 1.0))
        worker.write_text(_tool("old-2", 1.5))
        collector = _glob(folder)
        mark = collector.checkpoint()
        with server.open("a") as f:
            f.write(_tool("new-2", 4.0))
        with worker.open("a") as f:
            f.write(_tool("new-1", 3.0))

        window = collector.collect(since=mark)

        assert [tool.fn for tool in window.tools] == ["new-1", "new-2"]
        assert collector.collect(since=window.end).tools == []

    def test_hb15_a_file_that_appears_later_is_read_from_its_start(self, folder: Path) -> None:
        (folder / "server.jsonl").write_text(_tool("before", 1.0))
        collector = _glob(folder)
        mark = collector.checkpoint()
        (folder / "worker-2.jsonl").write_text(_tool("first", 2.0) + _tool("second", 2.5))

        window = collector.collect(since=mark)

        assert [tool.fn for tool in window.tools] == ["first", "second"]

    def test_hb15_a_bad_line_names_its_file_and_the_rest_is_read(self, folder: Path) -> None:
        (folder / "a.jsonl").write_text(_tool("good-a", 1.0))
        broken = folder / "b.jsonl"
        broken.write_text("not json\n" + _tool("good-b", 2.0))
        collector = _glob(folder)

        with pytest.raises(TraceFormatError) as info:
            collector.collect(since=0)

        assert str(info.value).startswith(f"{broken} line 1: not valid JSON")
        window = info.value.window
        assert window is not None
        assert [tool.fn for tool in window.tools] == ["good-a", "good-b"]
        # The next read starts after that line, or every read would stop on it.
        assert collector.collect(since=window.end).tools == []

    def test_hb15_each_process_declares_its_own_tools(self, folder: Path) -> None:
        (folder / "server.jsonl").write_text(_hello("get_order"))
        (folder / "worker.jsonl").write_text(_hello("send_email"))

        assert _glob(folder).declared() == frozenset({"get_order", "send_email"})

    def test_hb15_a_process_that_said_bye_declares_nothing(self, folder: Path) -> None:
        """An old build that declared refund and exited must not vouch for the running one,
        which may call refund without recording it."""
        (folder / "traces-7.jsonl").write_text(_hello("get_order", pid=7))
        (folder / "traces-3.jsonl").write_text(_hello("get_order", "refund", pid=3) + _bye(3))

        assert _glob(folder).declared() == frozenset({"get_order"})

    def test_hb15_a_file_reused_by_a_new_process_declares_its_tools(self, folder: Path) -> None:
        """In a container every main process is pid 1: the next one appends its hello."""
        (folder / "traces-1.jsonl").write_text(
            _hello("refund", pid=1) + _bye(1) + _hello("get_order", pid=1)
        )

        assert _glob(folder).declared() == frozenset({"get_order"})


class TestOneFileEachTime:
    """A link is a second name for a file: what it holds is read once (review of #52)."""

    def test_hb15_a_link_to_a_file_is_read_once(self, folder: Path) -> None:
        (folder / "traces-101.jsonl").write_text(_tool("lookup", 1.0))
        (folder / "latest.jsonl").symlink_to(folder / "traces-101.jsonl")

        window = _glob(folder).collect(since=0)

        assert [tool.fn for tool in window.tools] == ["lookup"]

    def test_hb15_a_link_made_during_the_run_reads_nothing_twice(self, folder: Path) -> None:
        traces = folder / "traces-101.jsonl"
        traces.write_text(_tool("before", 1.0))
        collector = _glob(folder)
        mark = collector.checkpoint()
        # "a" sorts before "traces-101": the link must not take the file's place.
        (folder / "a-latest.jsonl").symlink_to(traces)
        with traces.open("a") as f:
            f.write(_tool("after", 2.0))

        window = collector.collect(since=mark)

        assert [tool.fn for tool in window.tools] == ["after"]

    def test_hb15_a_hard_link_made_during_the_run_reads_nothing_twice(self, folder: Path) -> None:
        traces = folder / "traces-101.jsonl"
        traces.write_text(_tool("before", 1.0))
        collector = _glob(folder)
        mark = collector.checkpoint()
        (folder / "a-copy.jsonl").hardlink_to(traces)
        with traces.open("a") as f:
            f.write(_tool("after", 2.0))

        window = collector.collect(since=mark)

        assert [tool.fn for tool in window.tools] == ["after"]


class TestWhatMatches:
    def test_hb15_missing_until_a_file_matches(self, tmp_path: Path) -> None:
        collector = GlobTraceCollector(str(tmp_path / "traces" / "*.jsonl"))
        assert collector.missing()

        (tmp_path / "traces").mkdir()
        # Docker makes a directory for a bind mount whose file is not there yet.
        (tmp_path / "traces" / "mounted.jsonl").mkdir()
        assert collector.missing()
        assert collector.collect(since=0).tools == []

        (tmp_path / "traces" / "server.jsonl").write_text("")
        assert not collector.missing()

    def test_hb15_a_path_with_a_wildcard_reads_every_file_it_matches(self) -> None:
        assert isinstance(trace_collector("var/traces/*.jsonl"), GlobTraceCollector)
        assert isinstance(trace_collector("var/traces-?.jsonl"), GlobTraceCollector)
        assert isinstance(trace_collector("var/traces.jsonl"), FileTraceCollector)

    def test_hb15_a_file_whose_name_has_a_bracket_is_that_file(self, tmp_path: Path) -> None:
        """develop read it as a file: a path that names a file stays that file."""
        traces = tmp_path / "tr[1]" / "traces.jsonl"
        traces.parent.mkdir()
        traces.write_text(_tool("lookup", 1.0))

        collector = trace_collector(str(traces))

        assert isinstance(collector, FileTraceCollector)
        assert [tool.fn for tool in collector.collect(since=0).tools] == ["lookup"]
