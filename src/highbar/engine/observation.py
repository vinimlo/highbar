"""Which trace records belong to a test, and the proof that the agent saw it (spec D4).

A test that checks tool calls (``function_calls``, even ``[]``) only passes when
there is evidence that the instrumented agent handled its message. Without that,
"no tool was called" cannot be told apart from "the message reached something that
writes no traces": an old server on the port, or an agent whose SDK never started.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

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


class Selection(NamedTuple):
    """What ``select_records`` found in a window."""

    window: Window  # the records that count for the test
    has_context: bool  # some record in the window carried context
    matched: bool  # some record matched the correlation: proof the test was seen


def select_records(window: Window, correlate: Mapping[str, Any]) -> Selection:
    """The records of ``window`` that count for the test.

    A record whose context names another test is left out: that is a late call of an
    earlier test (A4). A record with no context is kept. It may well be this test's,
    from a thread the agent started without copying the context, and dropping it would
    let ``function_calls: []`` pass with the call made. When no record carries context
    at all (the decorator without middleware), the whole window counts.
    """
    has_context = any(t.context for t in window.tools) or any(t.context for t in window.turns)
    if not has_context:
        return Selection(window, has_context=False, matched=False)

    def belongs(context: Mapping[str, Any]) -> bool:
        return all(
            key in context and same_value(context[key], value) for key, value in correlate.items()
        )

    tools = [t for t in window.tools if not t.context or belongs(t.context)]
    turns = [t for t in window.turns if not t.context or belongs(t.context)]
    matched = any(t.context and belongs(t.context) for t in [*window.tools, *window.turns])
    return Selection(
        Window(tools=tools, turns=turns, hellos=window.hellos),
        has_context=True,
        matched=matched,
    )


def describe(correlate: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in correlate.items())
