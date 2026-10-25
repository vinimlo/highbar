"""The correlation context of the message being handled (``test``, ``conversation_id``...).

A ``ContextVar``, so concurrent requests in one process (async tasks, threads that
copy the context) each see their own.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar(
    "highbar_context", default=MappingProxyType({})
)


def current() -> dict[str, Any]:
    return dict(_CONTEXT.get())


def merged(extra: Mapping[str, Any]) -> dict[str, Any]:
    """The current context plus ``extra``, without keys whose value is ``None``.

    ``test=request.headers.get("x-highbar-test")`` without the header must not become
    ``test: null``, which would correlate with nothing.
    """
    return {**current(), **{key: value for key, value in extra.items() if value is not None}}


@contextmanager
def pushed(extra: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Make ``merged(extra)`` the context until the block ends."""
    context = merged(extra)
    previous = _CONTEXT.get()
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        try:
            _CONTEXT.reset(token)
        except ValueError:
            # The block ended in another copy of the context than it began in: a sync
            # generator that Starlette steps one next() per copied context.
            _CONTEXT.set(previous)
