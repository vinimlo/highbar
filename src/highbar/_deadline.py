"""A wait with a deadline for the whole of an HTTP exchange (HB-39)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


def within[T](
    task: Callable[[], T], seconds: float, late: Callable[[], BaseException], *, name: str
) -> T:
    """``task`` in a thread of its own, waited for ``seconds`` at most.

    httpx's timeout holds for each read, so a server that sends its status line and
    headers a byte at a time never trips it. Past the deadline the thread is left to end
    on its own, as a Python hook abandoned at its timeout is, and ``late()`` is raised.
    What ``task`` raises in time is raised here, in the caller's thread.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = task()
        except BaseException as e:  # handed to the caller, in the caller's thread
            outcome["error"] = e

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise late()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]
