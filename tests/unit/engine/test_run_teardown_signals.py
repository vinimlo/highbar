"""HB-10 v1: a signal that lands while agent.run's teardown puts back waits for its end.

A Ctrl-C there, after a run that ended normally or a second one after the first, left
what the setup changed on a shared bot. The teardown runs to its end within
agent.timeout, and the signal goes where it would have gone after it.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.engine.session import RunHookError
from tests.unit.engine.test_run_hooks import _run, _RunHooks


class _SignalledTeardown(_RunHooks):
    """A teardown that gets ``sig`` halfway, as when Ctrl-C lands while it puts back."""

    def __init__(self, events: list[str], sig: signal.Signals) -> None:
        super().__init__(events)
        self._sig = sig

    def after_run(self) -> None:
        self._events.append("run.teardown")
        os.kill(os.getpid(), self._sig)
        time.sleep(0.05)  # where the signal's handler runs, if nothing holds it
        self._events.append("run.teardown done")


class TestSignalDuringTeardown:
    """HB-10 v1: a signal that lands while the teardown puts back waits for it to end."""

    def test_hb10_ctrl_c_during_the_teardown_waits_for_it(self) -> None:
        events: list[str] = []

        with pytest.raises(KeyboardInterrupt):
            _run(events, _SignalledTeardown(events, signal.SIGINT))

        assert events[-2:] == ["run.teardown", "run.teardown done"]

    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
    def test_hb10_sigterm_during_the_teardown_waits_for_it(self, sig: signal.Signals) -> None:
        """The CLI turns these into SystemExit(128 + signal); the teardown ends first."""
        events: list[str] = []

        def exit_on(signum: int, frame: Any) -> None:
            raise SystemExit(128 + signum)

        previous = signal.signal(sig, exit_on)
        try:
            with pytest.raises(SystemExit) as info:
                _run(events, _SignalledTeardown(events, sig))
        finally:
            signal.signal(sig, previous)

        assert info.value.code == 128 + sig
        assert events[-2:] == ["run.teardown", "run.teardown done"]

    def test_hb10_a_teardown_that_fails_while_holding_a_signal_is_still_a_warning(
        self,
    ) -> None:
        """The warning is told before the held signal is delivered."""
        events: list[str] = []
        reporter = MagicMock()

        class Fails(_SignalledTeardown):
            def after_run(self) -> None:
                super().after_run()
                raise RunHookError("agent.run.teardown exited with code 2: flag service down")

        with pytest.raises(KeyboardInterrupt):
            _run(events, Fails(events, signal.SIGINT), reporter=reporter)

        (call,) = reporter.on_warning.call_args_list
        assert call.args[0].startswith("agent.run.teardown exited with code 2")

    def test_hb10_a_run_outside_the_main_thread_still_tears_down(self) -> None:
        """Only the main thread can hold signals: elsewhere the teardown runs as it is."""
        events: list[str] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                _run(events, _RunHooks(events))
            except BaseException as e:
                errors.append(e)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=10)

        assert errors == []
        assert events[-1] == "run.teardown"
