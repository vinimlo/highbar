"""Tool decorator — records each call of a sync or async function as a `tool` record.

Nothing here may change what the tool does for the agent: arguments are bound, and
failures described, without ever raising; the call itself is made exactly as the
agent made it.
"""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from highbar.sdk.core import Highbar


def make_tool_decorator(fn: Callable[..., Any], hb: Highbar) -> Callable[..., Any]:
    """Wrap ``fn`` so each call is recorded with its arguments, result and error.

    An ``async def`` gets an async wrapper that records after the ``await``: wrapping
    it like a plain function would record the coroutine object, with zero duration,
    and never see its exception (S1). The same goes for a coroutine returned by a
    plain function (an async tool under another, sync, decorator).

    Any exception counts as a failure, ``CancelledError`` included: caught as
    ``Exception`` only, a cancelled call would be recorded as a success returning null.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not hb.enabled:
                return await fn(*args, **kwargs)
            call = _Call(hb, fn, args, kwargs)
            try:
                coroutine = fn(*args, **kwargs)  # wrong arguments raise here, not on await
            except BaseException as e:
                call.failed(e)
                raise
            return await call.awaited(coroutine)

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not hb.enabled:
            return fn(*args, **kwargs)
        call = _Call(hb, fn, args, kwargs)
        try:
            result = fn(*args, **kwargs)
        except BaseException as e:
            call.failed(e)
            raise
        if inspect.iscoroutine(result):
            return call.awaited(result)
        call.returned(result)
        return result

    return wrapper


class _Call:
    """One call of a tool, recorded once when it returns or fails."""

    def __init__(
        self,
        hb: Highbar,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self._hb = hb
        self._name = fn.__name__
        self._args = _arguments(fn, args, kwargs)
        self._started_at = time.time()

    def returned(self, result: Any) -> None:
        self._record(result, None)

    def failed(self, error: BaseException) -> None:
        self._record(None, _describe(error))

    async def awaited(self, awaitable: Awaitable[Any]) -> Any:
        try:
            result = await awaitable
        except BaseException as e:
            self.failed(e)
            raise
        self.returned(result)
        return result

    def _record(self, result: Any, error: str | None) -> None:
        self._hb._record_call(self._name, self._args, result, error, self._started_at, time.time())


def _describe(error: BaseException) -> str:
    name = type(error).__name__
    try:
        message = str(error)
    except Exception:
        # An exception that cannot describe itself still failed the call.
        return name
    return f"{name}: {message}" if message else name


def _arguments(
    fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """The call's arguments by parameter name, or as given when they do not bind.

    They do not bind when the call is wrong (a model passing ``id=`` for
    ``account_id``: the attempt still has to be recorded), or when a decorator below
    this one injects arguments the signature it exposes asks for.
    """
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
    except (TypeError, ValueError):
        given = dict(kwargs)
        if args:
            given["*args"] = list(args)
        return given
    bound.apply_defaults()
    return dict(bound.arguments)
