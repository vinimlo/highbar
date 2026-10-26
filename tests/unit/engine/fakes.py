"""A fake agent for runner tests: the HTTP client and the trace collector in one.

Each send produces the records that an instrumented agent writes while handling the
message. By default that is one `turn` tagged with the test id the runner sent, as
highbar.asgi.Middleware writes. A script can also return a second window, written
late: it lands after the test's read, before the next message (or at the final read).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from highbar.domain.models import Hello, TurnRecord, Window
from highbar.engine.placeholders import Values

Script = Callable[[str, str], "Window | tuple[Window, Window]"]


def observed(message: str, test_id: str) -> Window:
    return Window(turns=[TurnRecord(ts=1.0, context={"test": test_id})])


class FakeAgent:
    def __init__(
        self,
        script: Script = observed,
        *,
        hello: bool = True,
        declares: tuple[str, ...] | None = None,
    ) -> None:
        self._script = script
        self._hellos = [Hello(ts=0.0, sdk="highbar-python/test", pid=1)] if hello else []
        # The tools the agent declares it records (spec D4), written when it starts.
        self._declarations = [declares] if declares is not None else []
        self._log: list[Window] = []
        self._late: list[Window] = []
        self._sent_since_read = False
        self.sent: list[tuple[str, str]] = []
        self.values: list[Values] = []
        self.path = Path("/tmp/fake-traces.jsonl")

    @property
    def test_ids(self) -> list[str]:
        return [test_id for _, test_id in self.sent]

    # The HTTP client side.
    def send(self, message: str, *, values: Values) -> str:
        self._arrive_late()
        self.sent.append((message, values.test_id))
        self.values.append(values)
        written = self._script(message, values.test_id)
        now, late = written if isinstance(written, tuple) else (written, None)
        self._log.append(now)
        if late is not None:
            self._late.append(late)
        self._sent_since_read = True
        return "ok"

    # The collector side.
    def checkpoint(self) -> int:
        return len(self._log)

    def collect(self, since: int) -> Window:
        if not self._sent_since_read:
            # Nothing was sent since the last read: time passed, late records are in.
            self._arrive_late()
        self._sent_since_read = False
        windows = self._log[since:]
        return Window(
            tools=[tool for window in windows for tool in window.tools],
            turns=[turn for window in windows for turn in window.turns],
            hellos=list(self._hellos),
            declarations=list(self._declarations),
            end=len(self._log),
        )

    def _arrive_late(self) -> None:
        self._log.extend(self._late)
        self._late = []
