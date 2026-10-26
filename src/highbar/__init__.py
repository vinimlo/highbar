"""highbar — evals for AI agents that behave like tests.

The public API is what this module exports: ``Highbar`` (the SDK), ``asgi`` (its
middleware) and ``run`` (run a spec from Python). Everything else is internal.

``import highbar`` loads only the standard library, so the SDK can live in an
agent's production image; ``run`` brings in the CLI's dependencies when first used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from highbar import asgi
from highbar.sdk.core import Highbar

if TYPE_CHECKING:
    from highbar.api import run

__all__ = ["Highbar", "asgi", "run"]


def __getattr__(name: str) -> Any:
    if name == "run":
        from highbar.api import run

        return run
    raise AttributeError(f"module 'highbar' has no attribute {name!r}")
