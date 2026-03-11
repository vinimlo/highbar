"""Core Gauntlet class — instantiation and tool registration."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from gauntlet.sdk.decorator import make_tool_decorator
from gauntlet.sdk.trace import TraceWriter

_ENV_VAR = "GAUNTLET_TRACES_URL"


class Gauntlet:
    """Main entry point for instrumenting agent tools.

    Usage:
        gt = Gauntlet()

        @gt.tool
        def search_orders(order_id: str) -> dict:
            ...
    """

    def __init__(self, traces: str | None = None) -> None:
        self._traces = traces or os.environ.get(_ENV_VAR)
        self._writer: TraceWriter | None = None
        self._tools: dict[str, Callable[..., Any]] = {}

        if self._traces:
            self._writer = TraceWriter(self._traces)

    @property
    def traces(self) -> str | None:
        return self._traces

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    @property
    def tools(self) -> dict[str, Callable[..., Any]]:
        return dict(self._tools)

    def tool(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator that registers a tool and captures invocations."""
        wrapped = make_tool_decorator(fn, self._writer)
        self._tools[fn.__name__] = wrapped
        return wrapped
