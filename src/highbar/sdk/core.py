"""The Highbar SDK object: tools, funnel records and turns (trace contract v1).

Standard library only. The trace file comes from ``HIGHBAR_TRACES_URL``, read on
every write, so tracing turns on and off without restarting the agent; without it,
nothing is written and a tool call costs one environment lookup.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any, TypeVar

from highbar.sdk import context
from highbar.sdk.decorator import make_tool_decorator
from highbar.sdk.trace import TraceWriter

_ENV_VAR = "HIGHBAR_TRACES_URL"
SDK_NAME = "highbar-python"

F = TypeVar("F", bound=Callable[..., Any])


def _sdk_version() -> str:
    try:
        return version("highbar")
    except PackageNotFoundError:
        return "dev"


class TurnHandle:
    """Handed out by ``Highbar.turn``; set ``response`` to record what the agent said."""

    def __init__(self, message: str | None) -> None:
        self.message = message
        self.response: str | None = None


class Highbar:
    """Writes what the agent does to the trace file highbar reads.

    Usage::

        hb = Highbar()

        @hb.tool
        async def get_order(order_id: str) -> dict: ...

        hb.record("search_products", args, result=res, conversation_id=cid)

        with hb.turn(test=request.headers.get("x-highbar-test")):
            ...
    """

    def __init__(self, traces: str | None = None) -> None:
        self._explicit = traces
        self._writer = TraceWriter()
        self._tools: dict[str, Callable[..., Any]] = {}
        self._hello_lock = threading.Lock()
        self._hello_paths: set[str] = set()
        path = self.traces
        if path is not None:
            self._ensure_hello(path)

    @property
    def traces(self) -> str | None:
        """The trace file right now: the one given, or ``HIGHBAR_TRACES_URL``."""
        return self._explicit or os.environ.get(_ENV_VAR) or None

    @property
    def enabled(self) -> bool:
        return self.traces is not None

    @property
    def tools(self) -> dict[str, Callable[..., Any]]:
        return dict(self._tools)

    def tool(self, fn: F) -> F:
        """Decorator: record every call of ``fn`` (sync or async) as a `tool` record."""
        wrapped = make_tool_decorator(fn, self)
        self._tools[fn.__name__] = wrapped
        return wrapped  # type: ignore[return-value]

    def record(
        self,
        fn: str,
        args: Mapping[str, Any] | None = None,
        *,
        result: Any = None,
        error: str | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        **context_fields: Any,
    ) -> None:
        """Record a tool call made somewhere a decorator cannot reach (a dispatch funnel).

        Extra keyword arguments go to the record's ``context``. A failure the tool
        returned as a payload, without raising, belongs in ``error``: highbar never
        reads ``result`` to decide whether a call failed.
        """
        now = time.time()
        self._write_tool(
            fn,
            dict(args or {}),
            result,
            error,
            now if started_at is None else started_at,
            now if ended_at is None else ended_at,
            context.merged(context_fields),
        )

    @contextmanager
    def turn(self, *, message: str | None = None, **context_fields: Any) -> Iterator[TurnHandle]:
        """Mark the handling of one message: writes a `turn` record when the block ends.

        Tool calls inside the block carry the turn's context. The record is written even
        if the block raises: the message was received, which is what a turn proves.
        """
        handle = TurnHandle(message)
        if not self.enabled:
            # Nothing will be written, so there is no context to carry.
            yield handle
            return
        with context.pushed(context_fields) as current:
            try:
                yield handle
            finally:
                self._write_turn(current, message=handle.message, response=handle.response)

    # Internals shared with the decorator and highbar.asgi.

    def _record_call(
        self,
        fn: str,
        args: dict[str, Any],
        result: Any,
        error: str | None,
        started_at: float,
        ended_at: float,
    ) -> None:
        self._write_tool(fn, args, result, error, started_at, ended_at, context.current())

    def _write_tool(
        self,
        fn: str,
        args: dict[str, Any],
        result: Any,
        error: str | None,
        started_at: float,
        ended_at: float,
        record_context: dict[str, Any],
    ) -> None:
        record: dict[str, Any] = {
            "type": "tool",
            "ts": time.time(),
            "fn": fn,
            "args": args,
            "result": result,
            "started_at": started_at,
            "ended_at": ended_at,
        }
        if error is not None:
            record["error"] = error
        if record_context:
            record["context"] = record_context
        self._write(record)

    def _write_turn(
        self,
        turn_context: dict[str, Any],
        *,
        message: str | None = None,
        response: str | None = None,
    ) -> None:
        record: dict[str, Any] = {"type": "turn", "ts": time.time()}
        if turn_context:
            record["context"] = turn_context
        if message is not None:
            record["message"] = message
        if response is not None:
            record["response"] = response
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        path = self.traces
        if path is None:
            return
        self._ensure_hello(path)
        self._writer.write(path, record)

    def _ensure_hello(self, path: str) -> None:
        """Write `hello` the first time this file is seen: it proves the SDK is on."""
        with self._hello_lock:
            if path in self._hello_paths:
                return
            self._hello_paths.add(path)
        self._writer.write(
            path,
            {
                "type": "hello",
                "ts": time.time(),
                "sdk": f"{SDK_NAME}/{_sdk_version()}",
                "pid": os.getpid(),
            },
        )
