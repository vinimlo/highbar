"""HB-10: holding signals around agent.run's teardown leaves the handlers as it found them.

Review of #62: a signal that lands while the handlers are being set must not leave one
of them held for good (a host of highbar.run() would lose Ctrl-C), and a handler set
outside Python, which cannot be put back from here, is not replaced at all.
"""

from __future__ import annotations

import os
import signal
import sys
from typing import Any

import pytest

from highbar.engine.signals import holding_signals


def test_hb10_a_signal_while_the_handlers_are_set_leaves_none_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = signal.getsignal(signal.SIGINT)
    real = signal.signal
    landed: list[int] = []

    def sigterm_lands_while_its_handler_is_set(sig: int, handler: Any) -> Any:
        if sig == signal.SIGTERM and not landed:
            landed.append(sig)
            raise SystemExit(143)  # what the CLI's SIGTERM handler raises
        return real(sig, handler)

    monkeypatch.setattr(signal, "signal", sigterm_lands_while_its_handler_is_set)

    with pytest.raises(SystemExit), holding_signals():
        pass

    assert signal.getsignal(signal.SIGINT) is original


def test_hb10_a_handler_set_outside_python_is_not_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_get, real_set = signal.getsignal, signal.signal
    replaced: list[int] = []

    def getsignal(sig: int) -> Any:
        return None if sig == signal.SIGHUP else real_get(sig)

    def setsignal(sig: int, handler: Any) -> Any:
        replaced.append(sig)
        return None if sig == signal.SIGHUP else real_set(sig, handler)

    monkeypatch.setattr(signal, "getsignal", getsignal)
    monkeypatch.setattr(signal, "signal", setsignal)

    with holding_signals():
        pass

    assert signal.SIGHUP not in replaced
    assert signal.SIGINT in replaced


def test_hb44_the_first_held_signal_says_why_highbar_waits(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A Ctrl-C that waits in silence reads as a hang, and a kill -9 leaves the flag on."""
    with pytest.raises(KeyboardInterrupt), holding_signals(notice="Waiting for the teardown"):
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGINT)

    assert capfd.readouterr().err.count("Waiting for the teardown") == 1


def test_hb44_without_a_signal_nothing_is_said(capfd: pytest.CaptureFixture[str]) -> None:
    with holding_signals(notice="Waiting for the teardown"):
        pass

    assert "Waiting" not in capfd.readouterr().err


def test_hb44_a_signal_while_the_line_is_written_does_not_write_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #66: the second of two signals back to back can land inside the write."""
    from highbar.engine import signals

    said: list[str] = []

    def say(line: str) -> None:
        said.append(line)
        if len(said) == 1:
            os.kill(os.getpid(), signal.SIGINT)  # lands before the first handler returns

    monkeypatch.setattr(signals, "_say", say)

    with pytest.raises(KeyboardInterrupt), holding_signals(notice="Waiting for the teardown"):
        os.kill(os.getpid(), signal.SIGINT)

    assert said == ["Waiting for the teardown"]


def test_hb44_without_a_stderr_the_line_goes_nowhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #66: with fd 2 closed at exec, the next file highbar opens can be fd 2:
    the trace file, a log or a connection must not get the line."""
    written: list[int] = []
    monkeypatch.setattr(sys, "__stderr__", None)
    monkeypatch.setattr(os, "write", lambda fd, data: written.append(fd) or len(data))

    with pytest.raises(KeyboardInterrupt), holding_signals(notice="Waiting for the teardown"):
        os.kill(os.getpid(), signal.SIGINT)

    assert written == []
