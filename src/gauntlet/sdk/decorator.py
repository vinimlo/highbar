"""Tool decorator implementation — wraps functions to capture traces."""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Callable
from typing import Any

from gauntlet.sdk.trace import TraceWriter


def make_tool_decorator(
    fn: Callable[..., Any],
    writer: TraceWriter | None,
) -> Callable[..., Any]:
    """Create a wrapper that captures tool invocations as trace boxes."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if writer is None:
            return fn(*args, **kwargs)

        bound_args = _bind_arguments(fn, args, kwargs)

        started_at = time.time()
        error = None
        result = None
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            ended_at = time.time()
            writer.write(
                fn=fn.__name__,
                args=bound_args,
                result=result,
                started_at=started_at,
                ended_at=ended_at,
                error=error,
            )

        return result

    return wrapper


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
