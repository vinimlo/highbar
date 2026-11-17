"""Signals held while something must finish (HB-10).

``agent.run``'s teardown puts back what its setup changed on a shared bot. A Ctrl-C or a
SIGTERM that stopped it halfway would leave the bot as the run left it, with nothing
said, so the teardown runs to its end first, within ``agent.timeout``, and the signal is
delivered after it, to the handler it would have reached.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from types import FrameType
from typing import Any

HELD = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextmanager
def holding_signals(notice: str | None = None) -> Iterator[None]:
    """Hold Ctrl-C, SIGTERM and SIGHUP while the block runs, then deliver the first.

    ``notice`` goes to stderr once, on the first signal held: a wait that says nothing
    reads as a hang (HB-44). Only the main thread can set handlers: elsewhere the block
    runs as it is. A handler set outside Python cannot be put back from here, so that
    signal is not held.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: list[int] = []

    def hold(signum: int, frame: FrameType | None) -> None:
        # Noted first: a second signal can land while the line is written (review of #66).
        held.append(signum)
        if notice and len(held) == 1:
            _say(notice)

    # Each handler is noted before it is replaced, inside the try: a signal that lands
    # while they are being set leaves none of them held for good (review of #62).
    previous: dict[int, Any] = {}
    try:
        for sig in HELD:
            handler = signal.getsignal(sig)
            if handler is None:
                continue
            previous[sig] = handler
            signal.signal(sig, hold)
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if held:
            signal.raise_signal(held[0])


def say(line: str) -> None:
    """One line to stderr, as the notice of a held signal is said."""
    _say(line)


def _say(line: str) -> None:
    """Straight to stderr's file descriptor: the handler can land in the middle of a
    write to sys.stderr, and a buffered write from inside it would raise "reentrant call".

    With no stderr at start (fd 2 closed), Python leaves ``sys.__stderr__`` None, and fd 2
    may be the trace file or a connection opened since: the line goes nowhere then.
    """
    stream = sys.__stderr__
    if stream is None:
        return
    with suppress(OSError, ValueError):
        os.write(stream.fileno(), f"\n  {line}\n".encode(errors="replace"))
