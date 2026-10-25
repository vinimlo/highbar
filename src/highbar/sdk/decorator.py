"""Tool decorator — records each call of a sync or async function as a `tool` record."""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from highbar.sdk.core import Highbar


def make_tool_decorator(fn: Callable[..., Any], hb: Highbar) -> Callable[..., Any]:
    """Wrap ``fn`` so each call is recorded with its arguments, result and error.

    An ``async def`` gets an async wrapper that records after the ``await``: wrapping
    it like a plain function would record the coroutine object, with zero duration,
    and never see its exception (S1).

    Any exception counts as a failure, ``CancelledError`` included: caught as
    ``Exception`` only, a cancelled call would be recorded as a success returning null.
    """
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            if not hb.enabled:
                return await fn(*args, **kwargs)
            bound = _bind_arguments(fn, args, kwargs)
            started_at = time.time()
            result: Any = None
            error: str | None = None
            try:
                result = await fn(*args, **kwargs)
                return result
            except BaseException as e:
                error = _describe(e)
                raise
            finally:
                hb._record_call(fn.__name__, bound, result, error, started_at, time.time())

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not hb.enabled:
            return fn(*args, **kwargs)
        bound = _bind_arguments(fn, args, kwargs)
        started_at = time.time()
        result: Any = None
        error: str | None = None
        try:
            result = fn(*args, **kwargs)
            return result
        except BaseException as e:
            error = _describe(e)
            raise
        finally:
            hb._record_call(fn.__name__, bound, result, error, started_at, time.time())

    return wrapper


def _describe(error: BaseException) -> str:
    message = str(error)
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _bind_arguments(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Bind positional and keyword args to parameter names."""
    sig = inspect.signature(fn)
    bound = sig.bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)
