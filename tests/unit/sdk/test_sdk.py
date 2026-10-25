"""The Python SDK writes the trace contract v1 (spec D4, D7)."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import logging
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from highbar import Highbar

ENV = "HIGHBAR_TRACES_URL"


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _of(path: Path, kind: str) -> list[dict[str, Any]]:
    return [r for r in _records(path) if r["type"] == kind]


@pytest.fixture
def traces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "traces.jsonl"
    monkeypatch.setenv(ENV, str(path))
    return path


class TestHello:
    def test_hello_is_written_when_constructed_with_the_variable_set(self, traces: Path) -> None:
        Highbar()

        (hello,) = _records(traces)
        assert hello["v"] == 1
        assert hello["type"] == "hello"
        assert hello["pid"] == os.getpid()
        assert hello["sdk"].startswith("highbar-python/")
        assert isinstance(hello["ts"], float)

    def test_hello_is_written_once_per_file(self, traces: Path) -> None:
        hb = Highbar()
        hb.record("a")
        hb.record("b")

        assert [r["type"] for r in _records(traces)] == ["hello", "tool", "tool"]

    def test_variable_is_read_on_every_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tracing turns on and off without restarting the agent."""
        monkeypatch.delenv(ENV, raising=False)
        hb = Highbar()

        @hb.tool
        def ping() -> str:
            return "pong"

        assert not hb.enabled
        ping()

        path = tmp_path / "later.jsonl"
        monkeypatch.setenv(ENV, str(path))
        ping()

        assert hb.enabled
        assert [r["type"] for r in _records(path)] == ["hello", "tool"]

    def test_explicit_path_wins_over_the_variable(
        self, traces: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        explicit = tmp_path / "explicit.jsonl"
        hb = Highbar(traces=str(explicit))

        assert hb.traces == str(explicit)
        assert [r["type"] for r in _records(explicit)] == ["hello"]
        assert not traces.exists()


class TestTool:
    def test_tool_record_has_the_v1_fields(self, traces: Path) -> None:
        hb = Highbar()

        @hb.tool
        def get_order(order_id: str, verbose: bool = False) -> dict[str, str]:
            return {"status": "shipped"}

        assert get_order("7") == {"status": "shipped"}

        (record,) = _of(traces, "tool")
        assert record["v"] == 1
        assert record["fn"] == "get_order"
        assert record["args"] == {"order_id": "7", "verbose": False}
        assert record["result"] == {"status": "shipped"}
        assert record["started_at"] <= record["ended_at"] <= record["ts"]
        assert "error" not in record
        assert "context" not in record

    def test_decorator_keeps_name_and_registers_the_tool(self, traces: Path) -> None:
        hb = Highbar()

        @hb.tool
        def search_orders(q: str) -> list[str]:
            """Find orders."""
            return []

        assert search_orders.__name__ == "search_orders"
        assert search_orders.__doc__ == "Find orders."
        assert hb.tools == {"search_orders": search_orders}

    def test_sync_exception_is_recorded_and_raised(self, traces: Path) -> None:
        hb = Highbar()

        @hb.tool
        def fail() -> None:
            raise ValueError("no stock")

        with pytest.raises(ValueError, match="no stock"):
            fail()

        (record,) = _of(traces, "tool")
        assert record["error"] == "ValueError: no stock"
        assert record["result"] is None

    def test_s1_async_tool_records_the_awaited_result_and_duration(self, traces: Path) -> None:
        hb = Highbar()

        @hb.tool
        async def fetch_user(user_id: int) -> dict[str, int]:
            await asyncio.sleep(0.05)
            return {"id": user_id}

        assert inspect.iscoroutinefunction(fetch_user)
        assert asyncio.run(fetch_user(7)) == {"id": 7}

        (record,) = _of(traces, "tool")
        assert record["result"] == {"id": 7}
        assert record["args"] == {"user_id": 7}
        assert record["ended_at"] - record["started_at"] >= 0.05

    def test_s1_async_tool_exception_is_recorded_and_raised(self, traces: Path) -> None:
        hb = Highbar()

        @hb.tool
        async def fetch_user(user_id: int) -> None:
            raise LookupError("gone")

        with pytest.raises(LookupError):
            asyncio.run(fetch_user(7))

        (record,) = _of(traces, "tool")
        assert record["error"] == "LookupError: gone"

    def test_cancelled_async_tool_is_recorded_as_error(self, traces: Path) -> None:
        """CancelledError is a BaseException: caught as Exception only, it would look
        like a call that succeeded and returned null."""
        hb = Highbar()

        @hb.tool
        async def slow() -> str:
            await asyncio.sleep(10)
            return "done"

        async def cancel_it() -> None:
            task = asyncio.create_task(slow())
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_it())

        (record,) = _of(traces, "tool")
        assert record["error"] == "CancelledError"

    def test_disabled_sdk_writes_nothing_and_calls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV, raising=False)
        hb = Highbar()

        @hb.tool
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5
        assert list(tmp_path.iterdir()) == []


class TestRecord:
    def test_record_writes_a_tool_with_kwargs_as_context(self, traces: Path) -> None:
        hb = Highbar()

        hb.record("search_products", {"q": "x"}, result=[1], conversation_id=42)

        (record,) = _of(traces, "tool")
        assert record["fn"] == "search_products"
        assert record["args"] == {"q": "x"}
        assert record["result"] == [1]
        assert record["context"] == {"conversation_id": 42}

    def test_record_passes_error_and_times_through(self, traces: Path) -> None:
        hb = Highbar()

        hb.record("pay", {}, error="card declined", started_at=10.0, ended_at=10.5)

        (record,) = _of(traces, "tool")
        assert record["error"] == "card declined"
        assert (record["started_at"], record["ended_at"]) == (10.0, 10.5)

    def test_write_failure_never_reaches_the_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv(ENV, str(tmp_path / "missing-dir" / "traces.jsonl"))
        caplog.set_level(logging.WARNING, logger="highbar")
        hb = Highbar()

        @hb.tool
        def answer() -> int:
            return 42

        assert answer() == 42
        hb.record("x", {}, result=object())
        assert "could not write" in caplog.text

    def test_unserializable_values_are_written_as_text(self, traces: Path) -> None:
        hb = Highbar()

        hb.record("x", {"when": object()}, result={1, 2})

        (record,) = _of(traces, "tool")
        assert isinstance(record["args"]["when"], str)

    def test_concurrent_writes_never_interleave(self, traces: Path) -> None:
        hb = Highbar()

        def burst(worker: int) -> None:
            for n in range(200):
                hb.record("t", {"worker": worker, "n": n, "pad": "x" * 500})

        threads = [threading.Thread(target=burst, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(_of(traces, "tool")) == 1600


class TestTurn:
    def test_turn_writes_a_turn_and_tools_inside_inherit_its_context(self, traces: Path) -> None:
        hb = Highbar()

        @hb.tool
        def lookup() -> str:
            return "ok"

        with hb.turn(message="hi", test="t1", conversation_id=None) as turn:
            lookup()
            turn.response = "hello"

        (tool,) = _of(traces, "tool")
        (record,) = _of(traces, "turn")
        assert tool["context"] == {"test": "t1"}
        assert record["context"] == {"test": "t1"}
        assert (record["message"], record["response"]) == ("hi", "hello")

    def test_record_inside_a_turn_merges_both_contexts(self, traces: Path) -> None:
        hb = Highbar()

        with hb.turn(test="t1"):
            hb.record("search", {}, conversation_id=9)

        (tool,) = _of(traces, "tool")
        assert tool["context"] == {"test": "t1", "conversation_id": 9}

    def test_turn_is_written_even_when_the_handler_raises(self, traces: Path) -> None:
        hb = Highbar()

        with pytest.raises(RuntimeError), hb.turn(test="t1"):
            raise RuntimeError("handler broke")

        assert len(_of(traces, "turn")) == 1

    def test_context_ends_with_the_turn(self, traces: Path) -> None:
        hb = Highbar()

        with hb.turn(test="t1"):
            pass
        hb.record("after", {})

        (tool,) = _of(traces, "tool")
        assert "context" not in tool

    def test_turn_context_does_not_leak_between_async_tasks(self, traces: Path) -> None:
        hb = Highbar()

        @hb.tool
        async def step(label: str) -> str:
            await asyncio.sleep(0.01)
            return label

        async def handle(test_id: str) -> None:
            with hb.turn(test=test_id):
                await asyncio.sleep(0.01)
                await step(test_id)

        async def both() -> None:
            await asyncio.gather(handle("a"), handle("b"))

        asyncio.run(both())

        tools = _of(traces, "tool")
        assert sorted((t["args"]["label"], t["context"]["test"]) for t in tools) == [
            ("a", "a"),
            ("b", "b"),
        ]


class TestReviewFindings:
    """Paths the PR review found breaking the agent, or recording a failure as a success."""

    def test_injecting_decorator_still_works_with_tracing_on(self, traces: Path) -> None:
        """functools.wraps exposes the inner signature, which the caller never satisfies."""
        hb = Highbar()

        def with_db(fn: Any) -> Any:
            @functools.wraps(fn)
            def inner(*args: Any, **kwargs: Any) -> Any:
                return fn(*args, db="DB", **kwargs)

            return inner

        @hb.tool
        @with_db
        def get_order(order_id: str, db: str) -> dict[str, str]:
            return {"order_id": order_id, "db": db}

        assert get_order(order_id="7") == {"order_id": "7", "db": "DB"}
        (record,) = _of(traces, "tool")
        assert record["args"] == {"order_id": "7"}
        assert "error" not in record

    def test_call_with_a_wrong_argument_is_recorded_and_raises(self, traces: Path) -> None:
        """A model calls delete_account(id=1): the attempt is the finding, not a non-event."""
        hb = Highbar()

        @hb.tool
        def delete_account(account_id: int) -> str:
            return "deleted"

        with pytest.raises(TypeError):
            delete_account(id=1)  # type: ignore[call-arg]

        (record,) = _of(traces, "tool")
        assert record["fn"] == "delete_account"
        assert record["args"] == {"id": 1}
        assert record["error"].startswith("TypeError")

    def test_value_whose_str_raises_never_reaches_the_agent(self, traces: Path) -> None:
        """An ORM row detached from its session raises in __repr__."""

        class Detached:
            def __repr__(self) -> str:
                raise RuntimeError("Instance is not bound to a Session")

            __str__ = __repr__

        hb = Highbar()
        row = Detached()

        @hb.tool
        def load_customer(cid: int) -> Detached:
            return row

        assert load_customer(1) is row
        hb.record("x", {"row": row})
        tools = _of(traces, "tool")
        assert tools[0]["result"] == "<unserializable Detached>"
        assert tools[1]["args"] == {"row": "<unserializable Detached>"}

    def test_deeply_nested_value_never_reaches_the_agent(
        self, traces: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger="highbar")
        deep: list[Any] = []
        for _ in range(100_000):
            deep = [deep]
        hb = Highbar()

        @hb.tool
        def build() -> list[Any]:
            return deep

        assert build() is deep
        assert "could not write" in caplog.text

    def test_exception_whose_str_raises_is_still_recorded_as_error(self, traces: Path) -> None:
        class Weird(Exception):
            def __str__(self) -> str:
                raise ValueError("cannot render")

        hb = Highbar()

        @hb.tool
        def flaky() -> None:
            raise Weird()

        with pytest.raises(Weird):
            flaky()

        (record,) = _of(traces, "tool")
        assert record["error"] == "Weird"

    def test_async_tool_behind_a_sync_decorator_records_the_awaited_outcome(
        self, traces: Path
    ) -> None:
        """@hb.tool over a plain wraps() decorator of an async def sees a coroutine."""

        def logged(fn: Any) -> Any:
            @functools.wraps(fn)
            def inner(*args: Any, **kwargs: Any) -> Any:
                return fn(*args, **kwargs)

            return inner

        hb = Highbar()

        @hb.tool
        @logged
        async def get_order(order_id: str) -> dict[str, str]:
            await asyncio.sleep(0.02)
            if order_id == "bad":
                raise TimeoutError("upstream timed out")
            return {"order_id": order_id}

        assert asyncio.run(get_order("7")) == {"order_id": "7"}
        with pytest.raises(TimeoutError):
            asyncio.run(get_order("bad"))

        ok, failed = _of(traces, "tool")
        assert ok["result"] == {"order_id": "7"}
        assert ok["ended_at"] - ok["started_at"] >= 0.02
        assert failed["error"] == "TimeoutError: upstream timed out"

    @pytest.mark.parametrize("enabled", [True, False])
    def test_turn_in_a_generator_stepped_in_copied_contexts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
    ) -> None:
        """Starlette steps a sync streaming body with each next() in a copy of the context."""
        if enabled:
            monkeypatch.setenv(ENV, str(tmp_path / "t.jsonl"))
        else:
            monkeypatch.delenv(ENV, raising=False)
        hb = Highbar()

        def stream() -> Iterator[str]:
            with hb.turn(test="t1"):
                yield "a"
                yield "b"

        chunks = stream()
        received = []
        while True:
            try:
                received.append(contextvars.copy_context().run(next, chunks))
            except StopIteration:
                break

        assert received == ["a", "b"]
