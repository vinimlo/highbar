"""Which trace records belong to a test, and the proof that the agent saw it (spec D4).

A test that checks tool calls (``function_calls``, even ``[]``) only passes when
there is evidence that the instrumented agent handled its message. Without that,
"no tool was called" cannot be told apart from "the message reached something that
writes no traces": an old server on the port, or an agent whose SDK never started.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from highbar.domain.models import Window

# How long a managed agent has, after its health check, to show its `hello`.
HELLO_GRACE_S = 2.0


class AgentNotObservedError(RuntimeError):
    """The agent answered, but the trace file shows no sign of its SDK."""


def same_value(a: Any, b: Any) -> bool:
    """Equal, or two non-boolean scalars with the same text: ``42`` and ``"42"``.

    A session returns ``conversation_id: 42`` and an emitter (or a header) writes
    ``"42"``. ``True`` is never ``1``, and containers compare only by equality.
    """
    if a == b and type(a) is type(b):
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    scalars = (str, int, float)
    return isinstance(a, scalars) and isinstance(b, scalars) and str(a) == str(b)


def select_records(window: Window, correlate: Mapping[str, Any]) -> tuple[Window, bool]:
    """The records of ``window`` that belong to the test, and whether any had context.

    When no record carries context (the decorator without middleware), the whole
    window is used, as before correlation existed.
    """
    has_context = any(t.context for t in window.tools) or any(t.context for t in window.turns)
    if not has_context:
        return window, False

    def belongs(context: Mapping[str, Any]) -> bool:
        return all(
            key in context and same_value(context[key], value) for key, value in correlate.items()
        )

    return (
        Window(
            tools=[t for t in window.tools if belongs(t.context)],
            turns=[t for t in window.turns if belongs(t.context)],
            hellos=window.hellos,
        ),
        True,
    )


def describe(correlate: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in correlate.items())
