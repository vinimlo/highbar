"""Tests for trace writing, fingerprinting, and sequence numbering."""

import json
import threading
from pathlib import Path

from gauntlet.sdk.trace import TraceWriter


class TestTraceWriter:
    def test_write_creates_jsonl_file(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "traces.jsonl"
        writer = TraceWriter(str(trace_path))

        writer.write(
            fn="search_orders",
            args={"order_id": "123"},
            result={"status": "shipped"},
        )

        assert trace_path.exists()
        lines = trace_path.read_text().strip().split("\n")
        assert len(lines) == 1

        data = json.loads(lines[0])
        assert data["fn"] == "search_orders"
        assert data["args"] == {"order_id": "123"}
        assert data["result"] == {"status": "shipped"}
        assert data["seq"] == 0
        assert isinstance(data["id"], str)
        assert len(data["id"]) == 16
        assert isinstance(data["started_at"], float)
        assert isinstance(data["ended_at"], float)

    def test_seq_increments(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "traces.jsonl"
        writer = TraceWriter(str(trace_path))

        writer.write(fn="fn_a", args={}, result=None)
        writer.write(fn="fn_b", args={}, result=None)
        writer.write(fn="fn_c", args={}, result=None)

        lines = trace_path.read_text().strip().split("\n")
        seqs = [json.loads(line)["seq"] for line in lines]
        assert seqs == [0, 1, 2]

    def test_fingerprints_are_unique(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "traces.jsonl"
        writer = TraceWriter(str(trace_path))

        writer.write(fn="search", args={"q": "test"}, result=None)
        writer.write(fn="search", args={"q": "test"}, result=None)

        lines = trace_path.read_text().strip().split("\n")
        ids = [json.loads(line)["id"] for line in lines]
        assert ids[0] != ids[1]

    def test_append_mode(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "traces.jsonl"

        writer1 = TraceWriter(str(trace_path))
        writer1.write(fn="fn_a", args={}, result=None)

        writer2 = TraceWriter(str(trace_path))
        writer2.write(fn="fn_b", args={}, result=None)

        lines = trace_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_write_with_timing(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "traces.jsonl"
        writer = TraceWriter(str(trace_path))

        writer.write(fn="slow_fn", args={}, result=None)

        data = json.loads(trace_path.read_text().strip())
        assert data["ended_at"] >= data["started_at"]

    def test_concurrent_writes(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "traces.jsonl"
        writer = TraceWriter(str(trace_path))

        num_threads = 5
        writes_per_thread = 10

        def write_traces() -> None:
            for i in range(writes_per_thread):
                writer.write(fn=f"fn_{i}", args={"i": i}, result=None)

        threads = [threading.Thread(target=write_traces) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = trace_path.read_text().strip().split("\n")
        assert len(lines) == 50

        seqs: list[int] = []
        for line in lines:
            data = json.loads(line)
            seqs.append(data["seq"])

        assert sorted(seqs) == list(range(50))
