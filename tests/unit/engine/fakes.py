"""A fake agent for runner tests: the HTTP client and the trace collector in one.

Each send produces the window of records that the next collect returns, the way an
instrumented agent writes them while handling the message. By default that is one
`turn` tagged with the test id the runner sent, as highbar.asgi.Middleware writes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from highbar.domain.models import Hello, TurnRecord, Window

Script = Callable[[str, str], Window]


def observed(message: str, test_id: str) -> Window:
    return Window(turns=[TurnRecord(ts=1.0, context={"test": test_id})])


class FakeAgent:
    def __init__(self, script: Script = observed, *, hello: bool = True) -> None:
        self._script = script
        self._hellos = [Hello(ts=0.0, sdk="highbar-python/test", pid=1)] if hello else []
        self._last = Window()
        self.sent: list[tuple[str, str]] = []
        self.path = Path("/tmp/fake-traces.jsonl")

    @property
    def test_ids(self) -> list[str]:
        return [test_id for _, test_id in self.sent]

    # The HTTP client side.
    def send(self, message: str, *, test_id: str) -> str:
        self.sent.append((message, test_id))
        self._last = self._script(message, test_id)
        return "ok"

    # The collector side.
    def checkpoint(self) -> int:
        return len(self.sent)

    def collect(self, since: int) -> Window:
        return Window(
            tools=list(self._last.tools),
            turns=list(self._last.turns),
            hellos=self._hellos + list(self._last.hellos),
        )
