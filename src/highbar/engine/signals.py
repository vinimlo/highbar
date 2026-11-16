"""Signals held while something must finish (HB-10).

``agent.run``'s teardown puts back what its setup changed on a shared bot. A Ctrl-C or a
SIGTERM that stopped it halfway would leave the bot as the run left it, with nothing
said, so the teardown runs to its end first, within ``agent.timeout``, and the signal is
delivered after it, to the handler it would have reached.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

HELD = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextmanager
def holding_signals() -> Iterator[None]:
    """Hold Ctrl-C, SIGTERM and SIGHUP while the block runs, then deliver the first.

    Only the main thread can set handlers: elsewhere the block runs as it is.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: list[int] = []

    def hold(signum: int, frame: FrameType | None) -> None:
        held.append(signum)

    previous = {sig: signal.signal(sig, hold) for sig in HELD}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            # None: a handler set outside Python, which cannot be put back from here.
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)
        if held:
            signal.raise_signal(held[0])
