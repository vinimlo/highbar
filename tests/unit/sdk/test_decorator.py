"""Tests for @gt.tool decorator behavior."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from gauntlet.sdk.core import Gauntlet


class TestToolDecorator:
    def test_decorated_function_still_works(self) -> None:
        gt = Gauntlet()

        @gt.tool
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5

    def test_decorator_preserves_function_name(self) -> None:
        gt = Gauntlet()

        @gt.tool
        def my_function() -> str:
            return "hello"

        assert my_function.__name__ == "my_function"

    def test_noop_without_traces_url(self) -> None:
        gt = Gauntlet()

        @gt.tool
        def search(q: str) -> str:
            return f"results for {q}"

        result = search(q="test")
        assert result == "results for test"

    def test_writes_trace_when_enabled(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        gt = Gauntlet(traces=str(trace_file))

        @gt.tool
        def search_orders(order_id: str) -> dict[str, str]:
            return {"order_id": order_id, "status": "shipped"}

        result = search_orders(order_id="12345")
        assert result == {"order_id": "12345", "status": "shipped"}

        lines = trace_file.read_text().strip().split("\n")
        assert len(lines) == 1

        trace = json.loads(lines[0])
        assert trace["fn"] == "search_orders"
        assert trace["args"] == {"order_id": "12345"}
        assert trace["result"] == {"order_id": "12345", "status": "shipped"}

    def test_reads_traces_from_env(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        with patch.dict("os.environ", {"GAUNTLET_TRACES_URL": str(trace_file)}):
            gt = Gauntlet()

            @gt.tool
            def ping() -> str:
                return "pong"

            ping()

        lines = trace_file.read_text().strip().split("\n")
        assert len(lines) == 1

    def test_multiple_tools_same_instance(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        gt = Gauntlet(traces=str(trace_file))

        @gt.tool
        def tool_a() -> str:
            return "a"

        @gt.tool
        def tool_b() -> str:
            return "b"

        tool_a()
        tool_b()
        tool_a()

        lines = trace_file.read_text().strip().split("\n")
        assert len(lines) == 3
        fns = [json.loads(line)["fn"] for line in lines]
        assert fns == ["tool_a", "tool_b", "tool_a"]

    def test_captures_kwargs(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        gt = Gauntlet(traces=str(trace_file))

        @gt.tool
        def search(query: str, limit: int = 10) -> list[str]:
            return ["result"]

        search(query="test", limit=5)

        trace = json.loads(trace_file.read_text().strip())
        assert trace["args"]["query"] == "test"
        assert trace["args"]["limit"] == 5

    def test_exception_still_raises(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        gt = Gauntlet(traces=str(trace_file))

        @gt.tool
        def failing() -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            failing()

    def test_exception_writes_trace(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        gt = Gauntlet(traces=str(trace_file))

        @gt.tool
        def failing() -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError):
            failing()

        lines = trace_file.read_text().strip().split("\n")
        assert len(lines) == 1

        trace = json.loads(lines[0])
        assert trace["fn"] == "failing"
        assert "error" in trace
        assert trace["error"] == "ValueError: boom"

    def test_exception_trace_has_null_result(self, tmp_path: Path) -> None:
        trace_file = tmp_path / "traces.jsonl"
        gt = Gauntlet(traces=str(trace_file))

        @gt.tool
        def failing() -> str:
            raise RuntimeError("oops")

        with pytest.raises(RuntimeError):
            failing()

        trace = json.loads(trace_file.read_text().strip())
        assert trace["result"] is None
