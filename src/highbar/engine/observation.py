"""Which trace records belong to a test, and the proof that the agent saw it (spec D4).

A test that checks tool calls (``function_calls``, even ``[]``) only passes when
there is evidence that the instrumented agent handled its message. Without that,
"no tool was called" cannot be told apart from "the message reached something that
writes no traces": an old server on the port, or an agent whose SDK never started.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
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


class Attribution(Enum):
    MINE = "mine"  # every correlation key present and equal
    OTHER = "other"  # some key present with another value: another execution's
    UNATTRIBUTED = "unattributed"  # no key with a value: nothing says whose it is


def attribute(context: Mapping[str, Any], correlate: Mapping[str, Any]) -> Attribution:
    """Whose a record is, by its context. A key whose value is null counts as absent.

    A record that agrees on some keys and lacks the others is unattributed, not
    mine: a thread that kept ``conversation_id`` but lost ``test`` may be this
    test's, or the one before it in the same conversation.
    """
    present = {key: context[key] for key in correlate if context.get(key) is not None}
    if not present:
        return Attribution.UNATTRIBUTED
    if any(not same_value(value, correlate[key]) for key, value in present.items()):
        return Attribution.OTHER
    return Attribution.MINE if len(present) == len(correlate) else Attribution.UNATTRIBUTED


class Classified(NamedTuple):
    """The records of a window, split by whose they are."""

    mine: Window
    other: Window
    unattributed: Window

    @property
    def correlated(self) -> bool:
        """Some record carries the correlation, so the window can be split by test.

        When none does (the decorator without the middleware), the window is all
        there is to go by, and every record in it counts for the test.
        """
        return any([self.mine.tools, self.mine.turns, self.other.tools, self.other.turns])


def classify(window: Window, correlate: Mapping[str, Any]) -> Classified:
    parts = {
        Attribution.MINE: Window(hellos=window.hellos, end=window.end),
        Attribution.OTHER: Window(),
        Attribution.UNATTRIBUTED: Window(),
    }
    for tool in window.tools:
        parts[attribute(tool.context, correlate)].tools.append(tool)
    for turn in window.turns:
        parts[attribute(turn.context, correlate)].turns.append(turn)
    return Classified(
        parts[Attribution.MINE], parts[Attribution.OTHER], parts[Attribution.UNATTRIBUTED]
    )


def describe(correlate: Mapping[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in correlate.items())
