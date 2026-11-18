"""HB-8: --jobs N, up to N tests in flight at once.

Only the send and the wait run in parallel. Records are attributed by their correlation,
never by which window they were read in, so an untagged record is never counted on
faith, and a late one still counts for the test that made it. Results reach the
reporter in spec order, and the stops (--max-cost, --max-errors, Ctrl-C) stop starting
tests.
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.adapters.file_collector import TraceFormatError
from highbar.adapters.http_client import AgentResponseError, InfraCause
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    BUDGET_EXCEEDED,
    REPEATED_ERRORS,
    AgentConfig,
    Budget,
    Expect,
    FunctionCallExpect,
    Hello,
    PythonHook,
    ResponseExpect,
    RunSummary,
    SessionConfig,
    Spec,
    Status,
    Suite,
    Trace,
    TurnRecord,
    Usage,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.placeholders import Values
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.session import SessionContext, SessionHooks
from tests.unit.engine.test_run_hooks import _RunHooks

ENDPOINT = "http://127.0.0.1:1/chat"
SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))
CALLS_LOOKUP = Expect(function_calls=[FunctionCallExpect(name="lookup")])
NO_CALLS = Expect(function_calls=[])
UNTAGGED = (
    "not observed: with --jobs, every record must carry agent.correlate "
    "(untagged records cannot be told apart)"
)
# A trace line that cannot be read: the collector raises when a read reaches it.
BAD_LINE = Window()

Script = Callable[["_Agent", str, str], "str | None"]


def _turn(test_id: str, usage: Usage | None = None) -> Window:
    return Window(turns=[TurnRecord(ts=1.0, context={"test": test_id}, usage=usage)])


def _call(fn: str, test_id: str | None = None) -> Window:
    context = {"test": test_id} if test_id is not None else {}
    trace = Trace(fn=fn, args={}, result=None, started_at=1.0, ended_at=1.0, context=context)
    return Window(tools=[trace])


class _Agent:
    """A fake agent for many senders at once: the HTTP client and the trace collector.

    ``script(agent, message, test_id)`` runs in the sender's thread for each message, and
    writes what the agent records with ``agent.write``; it returns the reply, or None
    for "ok".
    """

    def __init__(self, script: Script) -> None:
        self._script = script
        self._lock = threading.Lock()
        self._windows: list[Window] = []
        self.ids: dict[str, str] = {}  # message -> its test id
        self.sent: list[str] = []
        self.done: list[str] = []  # in the order the sends ended
        self.reads: list[Window] = []
        self.most_in_flight = 0
        self._flying = 0
        self.after_read: Callable[[Window], None] | None = None
        self.before_read: Callable[[], None] | None = None
        self.threads: dict[str, threading.Thread] = {}  # message -> the thread that sent it
        self.path = Path("/tmp/fake-traces.jsonl")

    def send(self, message: str, *, values: Values) -> str:
        with self._lock:
            self.ids[message] = values.test_id
            self.threads[message] = threading.current_thread()
            self.sent.append(message)
            self._flying += 1
            self.most_in_flight = max(self.most_in_flight, self._flying)
        try:
            reply = self._script(self, message, values.test_id)
        finally:
            with self._lock:
                self._flying -= 1
                self.done.append(message)
        return "ok" if reply is None else reply

    def write(self, *windows: Window) -> None:
        with self._lock:
            self._windows.extend(windows)

    def checkpoint(self) -> int:
        with self._lock:
            return len(self._windows)

    def collect(self, since: int) -> Window:
        if self.before_read is not None:
            self.before_read()
        with self._lock:
            windows = self._windows[since:]
            end = len(self._windows)
        bad = any(window is BAD_LINE for window in windows)
        window = Window(
            tools=[tool for w in windows for tool in w.tools],
            turns=[turn for w in windows for turn in w.turns],
            hellos=[Hello(ts=0.0, sdk="highbar-python/test", pid=1)],
            end=end,
        )
        self.reads.append(window)
        if self.after_read is not None:
            self.after_read(window)
        if bad:
            raise TraceFormatError("/tmp/fake-traces.jsonl line 3: not JSON", window)
        return window

    def missing(self) -> bool:
        return False

    def declared(self) -> frozenset[str] | None:
        return None


def _runner(
    agent: _Agent, *, jobs: int, reporter: MagicMock | None = None, **options: Any
) -> Runner:
    return Runner(
        adapter=options.pop("adapter", None) or MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=options.pop("settle_time", 0),
        jobs=jobs,
        **options,
    )


def _spec(*tests: Case) -> Spec:
    return Spec(
        agent=AgentConfig(start="agent", endpoint=ENDPOINT),
        suites=[Suite(name="S", tests=list(tests))],
    )


def _verdicts(summary: RunSummary) -> list[tuple[str, Status, tuple[str, ...]]]:
    return [(test.name, test.status, test.errors) for test in summary.tests]


def _seen(agent: _Agent, message: str, event: threading.Event) -> None:
    """Set ``event`` once a read has taken in the `turn` of ``message``."""

    def check(window: Window) -> None:
        test_id = agent.ids.get(message)
        if test_id is not None and any(t.context.get("test") == test_id for t in window.turns):
            event.set()

    agent.after_read = check


class TestInParallel:
    def test_hb8_three_tests_at_jobs_3_take_one_send_and_end_as_at_jobs_1(self) -> None:
        def script(agent: _Agent, message: str, test_id: str) -> None:
            time.sleep(0.5)
            agent.write(_turn(test_id), _call("delete" if message == "b" else "lookup", test_id))

        tests = [
            Case(trigger="a", expect=CALLS_LOOKUP),
            Case(trigger="b", expect=Expect(forbid=("delete",))),
            Case(trigger="c", expect=Expect(function_calls=CALLS_LOOKUP.function_calls)),
        ]
        # watch=0: the test that forbids "delete" would hold the end of the run for
        # agent.watch (HB-53), and this measures only the sends in flight together.
        one = _runner(_Agent(script), jobs=1, watch=0).run(_spec(*tests))

        started = time.monotonic()
        three = _runner(_Agent(script), jobs=3, watch=0).run(_spec(*tests))
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"3 tests of 0.5 s each took {elapsed:.2f} s at --jobs 3"
        assert [test.status for test in three.tests] == [
            Status.PASSED,
            Status.FAILED,
            Status.PASSED,
        ]
        assert _verdicts(three) == _verdicts(one)

    def test_hb8_results_reach_the_reporter_in_spec_order_when_the_second_ends_first(
        self,
    ) -> None:
        def script(agent: _Agent, message: str, test_id: str) -> None:
            time.sleep(0.6 if message == "first" else 0.1)
            agent.write(_turn(test_id))

        agent, reporter = _Agent(script), MagicMock()

        _runner(agent, jobs=2, reporter=reporter).run(
            _spec(Case(trigger="first", expect=SAYS_OK), Case(trigger="second", expect=SAYS_OK))
        )

        assert agent.done == ["second", "first"], "the second test was to end first"
        names = [call.args[0] for call in reporter.on_test_result.call_args_list]
        assert names == ["first", "second"]


class TestAttribution:
    def test_hb8_an_untagged_call_is_an_error_never_counted_for_the_test_it_lands_next_to(
        self,
    ) -> None:
        """b's agent records `lookup` with no test context while a's window is open: a
        expects `lookup`, and must not pass on b's call."""
        b_wrote = threading.Event()

        def script(agent: _Agent, message: str, test_id: str) -> None:
            if message == "a":
                b_wrote.wait(1)
            else:
                agent.write(_call("lookup"))
                b_wrote.set()

        summary = _runner(_Agent(script), jobs=2).run(
            _spec(Case(trigger="a", expect=CALLS_LOOKUP), Case(trigger="b", expect=NO_CALLS))
        )

        a, b = summary.tests
        assert (a.status, b.status) == (Status.ERROR, Status.ERROR)
        assert a.errors[0].startswith(UNTAGGED), a.errors
        assert b.errors[0].startswith(UNTAGGED), b.errors

    def test_hb8_a_late_call_tagged_for_a_read_while_b_is_in_flight_demotes_a_not_b(
        self,
    ) -> None:
        b_started, a_read = threading.Event(), threading.Event()
        overlapped: list[bool] = []

        def script(agent: _Agent, message: str, test_id: str) -> None:
            if message == "a":
                overlapped.append(b_started.wait(1))
                agent.write(_turn(test_id))
            else:
                b_started.set()
                a_read.wait(1)
                # a's own call, written after a's window closed, while b is in flight.
                agent.write(_call("delete", agent.ids["a"]), _turn(test_id))

        agent = _Agent(script)
        _seen(agent, "a", a_read)

        summary = _runner(agent, jobs=2).run(
            _spec(
                Case(trigger="a", expect=Expect(forbid=("delete",))),
                Case(trigger="b", expect=NO_CALLS),
            )
        )

        assert overlapped == [True], "b was to be in flight while a ran"
        a, b = summary.tests
        assert (a.status, b.status) == (Status.FAILED, Status.PASSED)
        assert "was recorded after the test's window closed" in a.errors[-1]

    def test_hb8_a_call_read_in_another_tests_window_counts_for_its_own_test(self) -> None:
        """b's `lookup`, tagged for b, is read by a's read while b waits: it is b's, in
        time, and a (no calls) does not fail on it."""
        b_called, a_read = threading.Event(), threading.Event()
        overlapped: list[bool] = []

        def script(agent: _Agent, message: str, test_id: str) -> None:
            if message == "a":
                overlapped.append(b_called.wait(1))
                agent.write(_turn(test_id))
            else:
                agent.write(_call("lookup", test_id))
                b_called.set()
                a_read.wait(1)
                agent.write(_turn(test_id))

        agent = _Agent(script)
        _seen(agent, "a", a_read)

        summary = _runner(agent, jobs=2).run(
            _spec(Case(trigger="a", expect=NO_CALLS), Case(trigger="b", expect=CALLS_LOOKUP))
        )

        assert overlapped == [True], "b's call was to be written while a ran"
        a, b = summary.tests
        assert (a.status, b.status) == (Status.PASSED, Status.PASSED), (a.errors, b.errors)
        (tool,) = b.phrasings[0].executions[0].turns[0].tools
        assert (tool.name, tool.late) == ("lookup", False)

    def test_hb8_an_unreadable_line_read_by_another_test_leaves_the_test_in_flight_unverified(
        self,
    ) -> None:
        """b's forbidden call is a line that cannot be read, and a's read reaches it: it
        may be b's call, so b cannot pass."""
        b_wrote, a_read = threading.Event(), threading.Event()
        overlapped: list[bool] = []

        def script(agent: _Agent, message: str, test_id: str) -> None:
            if message == "a":
                overlapped.append(b_wrote.wait(1))
                agent.write(_turn(test_id))
            else:
                agent.write(_turn(test_id), BAD_LINE)
                b_wrote.set()
                a_read.wait(1)

        agent = _Agent(script)
        _seen(agent, "a", a_read)

        summary = _runner(agent, jobs=2).run(
            _spec(
                Case(trigger="a", expect=Expect(forbid=("delete",))),
                Case(trigger="b", expect=Expect(forbid=("delete",))),
            )
        )

        assert overlapped == [True], "b's line was to be written while a ran"
        a, b = summary.tests
        assert (a.status, b.status) == (Status.ERROR, Status.ERROR)
        assert any("line 3: not JSON" in error for error in b.errors), b.errors


class TestRecordsNoReadCanGiveToOneTest:
    """Review of #95: a record with no context, read after its test ended, may be the
    late record of any test that ended; each one it would change is unverified, not only
    the last to start, which is the one a run in order suspects."""

    @pytest.mark.parametrize("kind", ["tool", "usage"])
    def test_hb8_a_late_untagged_record_read_in_a_later_window_leaves_its_test_unverified(
        self, kind: str
    ) -> None:
        """The reviewer's A/B/C/D: A's record comes 0.25 s after its turn, B is quick, and
        C and D take 1 s; C's read finds it, with A and B ended and D in flight."""
        spent = Usage(cost=0.05, currency="USD")

        def script(agent: _Agent, message: str, test_id: str) -> None:
            if message == "a":
                agent.write(_turn(test_id, spent))
                late = (
                    _call("delete_account")
                    if kind == "tool"
                    else Window(turns=[TurnRecord(ts=2.0, usage=Usage(cost=0.1, currency="USD"))])
                )
                threading.Timer(0.25, agent.write, [late]).start()
                return
            time.sleep(0.05 if message == "b" else 1.0)
            agent.write(_turn(test_id, spent), _call("get_order", test_id))

        guard = (
            Expect(forbid=("delete_account",))
            if kind == "tool"
            else Expect(budget=Budget(max_cost=0.1))
        )
        gets = Expect(function_calls=[FunctionCallExpect(name="get_order")])
        summary = _runner(_Agent(script), jobs=3).run(
            _spec(
                Case(trigger="a", expect=guard),
                *(Case(trigger=name, expect=gets) for name in "bcd"),
            )
        )

        statuses = [test.status for test in summary.tests]
        assert statuses == [Status.ERROR, Status.PASSED, Status.PASSED, Status.PASSED], [
            test.errors for test in summary.tests
        ]

    def test_hb8_a_late_untagged_call_read_after_the_last_test_leaves_its_test_unverified(
        self,
    ) -> None:
        """A's call comes 0.8 s after its message: after both reads, before the last one."""

        def script(agent: _Agent, message: str, test_id: str) -> None:
            agent.write(_turn(test_id))
            if message == "a":
                threading.Timer(0.8, agent.write, [_call("delete_account")]).start()
            else:
                time.sleep(0.05)

        summary = _runner(_Agent(script), jobs=2, settle_time=0.5).run(
            _spec(
                Case(trigger="a", expect=Expect(forbid=("delete_account",))),
                Case(trigger="b", expect=SAYS_OK),
            )
        )

        a, b = summary.tests
        assert (a.status, b.status) == (Status.ERROR, Status.PASSED), (a.errors, b.errors)

    def test_hb8_a_record_the_session_setup_causes_counts_for_its_test(self) -> None:
        """b's setup makes the agent record a call tagged for b, and a's read takes it in
        before b sends: it is b's, in time."""
        b_wrote, a_read = threading.Event(), threading.Event()
        overlapped: list[bool] = []

        def script(agent: _Agent, message: str, test_id: str) -> None:
            if message == "a":
                overlapped.append(b_wrote.wait(1))
            agent.write(_turn(test_id))

        agent = _Agent(script)
        _seen(agent, "a", a_read)

        class Opens(_RunHooks):
            def setup(self, ctx: Any) -> dict[str, Any]:
                if ctx.test_name == "b":
                    agent.write(_call("lookup", ctx.test_id))
                    b_wrote.set()
                    a_read.wait(1)
                return {}

        summary = _runner(agent, jobs=2, session=Opens([])).run(
            _spec(Case(trigger="a", expect=NO_CALLS), Case(trigger="b", expect=CALLS_LOOKUP))
        )

        assert overlapped == [True], "b's setup was to run while a ran"
        a, b = summary.tests
        assert (a.status, b.status) == (Status.PASSED, Status.PASSED), (a.errors, b.errors)


class TestStops:
    def test_hb8_max_cost_stops_dispatch_and_says_the_overshoot_and_the_tests_in_flight(
        self,
    ) -> None:
        """D4: the tests in flight finish, and the message says what they spent over."""
        usage = Usage(cost=0.25, currency="USD")

        def script(agent: _Agent, message: str, test_id: str) -> None:
            time.sleep(0.05 if message == "t1" else 0.4)
            agent.write(_turn(test_id, usage))

        agent = _Agent(script)

        summary = _runner(agent, jobs=3, max_cost=0.2).run(
            _spec(*(Case(trigger=f"t{i}", expect=SAYS_OK) for i in range(1, 5)))
        )

        assert sorted(agent.sent) == ["t1", "t2", "t3"]
        assert summary.stop_reason == BUDGET_EXCEEDED
        assert summary.stopped == (
            "--max-cost 0.2: the run spent 0.25 USD by the end of 't1', so it started no "
            "more tests; the 2 tests in flight then finished, and the run spent 0.75 USD in "
            "all, 0.55 USD over the ceiling; raise the ceiling to run the rest"
        )
        assert [test.name for test in summary.tests] == ["t1", "t2", "t3"]

    def test_hb8_max_cost_with_no_test_left_to_start_does_not_ask_to_run_the_rest(
        self,
    ) -> None:
        """HB-33 with D4: past the ceiling with every test already started, nothing was
        left out; the message still says what the tests in flight spent over it."""
        usage = Usage(cost=0.25, currency="USD")

        def script(agent: _Agent, message: str, test_id: str) -> None:
            time.sleep(0.05 if message == "t1" else 0.4)
            agent.write(_turn(test_id, usage))

        summary = _runner(_Agent(script), jobs=3, max_cost=0.2).run(
            _spec(*(Case(trigger=f"t{i}", expect=SAYS_OK) for i in range(1, 4)))
        )

        assert summary.stop_reason == BUDGET_EXCEEDED
        assert summary.stopped == (
            "--max-cost 0.2: the run spent 0.25 USD by the end of 't1'; the 2 tests in "
            "flight then finished, and the run spent 0.75 USD in all, 0.55 USD over its "
            "ceiling, with no test left to run"
        )

    def test_hb8_max_errors_counts_in_the_order_executions_end_and_stops_dispatch(
        self,
    ) -> None:
        def script(agent: _Agent, message: str, test_id: str) -> None:
            time.sleep(0.1)
            raise AgentResponseError(
                f"HTTP 500 from {ENDPOINT}: down",
                InfraCause("http_status", ENDPOINT, status=500),
            )

        agent = _Agent(script)

        summary = _runner(agent, jobs=2, max_errors=3).run(
            _spec(*(Case(trigger=f"t{i}", expect=SAYS_OK) for i in range(6)))
        )

        assert agent.most_in_flight == 2
        assert summary.stop_reason == REPEATED_ERRORS
        assert summary.stopped is not None
        assert summary.stopped.startswith(
            f"3 executions in a row failed with HTTP 500 from {ENDPOINT}"
        )
        # The third error stops dispatch: at most the one test in flight then also ran.
        assert len(agent.sent) <= 4
        assert all(test.status is Status.ERROR for test in summary.tests)

    def test_hb8_ctrl_c_waits_for_the_sends_in_flight_then_stops_the_agent_and_tears_down(
        self,
    ) -> None:
        events: list[str] = []
        b_started = threading.Event()

        def script(agent: _Agent, message: str, test_id: str) -> None:
            events.append(f"send {message}")
            if message == "a":
                b_started.wait(1)
                os.kill(os.getpid(), signal.SIGINT)
                # As a Ctrl-C from the terminal does, it reaches the main thread while
                # the message is still in flight.
                time.sleep(0.2)
            else:
                b_started.set()
                time.sleep(0.3)
            agent.write(_turn(test_id))
            events.append(f"sent {message}")

        adapter = MagicMock()
        adapter.start.side_effect = lambda: events.append("agent.start")
        adapter.stop.side_effect = lambda: events.append("agent.stop")
        runner = _runner(
            _Agent(script),
            jobs=2,
            adapter=adapter,
            session=_RunHooks(events),
            hook_timeout=5.0,
        )

        with pytest.raises(KeyboardInterrupt):
            runner.run(_spec(*(Case(trigger=name, expect=SAYS_OK) for name in "abc")))

        assert "sent b" in events, events
        assert "send c" not in events, events
        assert events.index("sent b") < events.index("agent.stop")
        assert events[-1] == "run.teardown"


class _Bug(BaseException):
    """What no code path in the runner catches: a bug in highbar itself."""


class TestReplyFromTurn:
    """--jobs with agent.reply: turn (HB-14): the wait for the turn is outside the lock,
    and its reads give each record to the test it names."""

    def test_hb8_a_reply_turn_read_by_another_tests_worker_still_reaches_its_test(
        self,
    ) -> None:
        read_by: list[str] = []

        def script(agent: _Agent, message: str, test_id: str) -> str:
            if message == "b":
                time.sleep(0.1)
                reply = TurnRecord(ts=1.0, context={"test": test_id}, response="ok b")
                agent.write(Window(turns=[reply]))
            return ""  # the body of a 202: the reply comes in the turn

        agent = _Agent(script)

        def before_read() -> None:
            # A's reply is written right before a read on b's thread: b's worker reads it.
            if agent.threads.get("b") is threading.current_thread() and not read_by:
                reply = TurnRecord(ts=2.0, context={"test": agent.ids["a"]}, response="ok a")
                agent.write(Window(turns=[reply]))
                read_by.append(threading.current_thread().name)

        agent.before_read = before_read

        summary = _runner(agent, jobs=2, reply_within=1.0).run(
            _spec(Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK))
        )

        assert read_by, "b's worker was to read a's reply"
        a, b = summary.tests
        assert (a.status, b.status) == (Status.PASSED, Status.PASSED), (a.errors, b.errors)
        assert a.phrasings[0].executions[0].turns[0].response == "ok a"

    def test_hb8_a_missing_reply_is_still_an_error_that_max_errors_counts(self) -> None:
        def script(agent: _Agent, message: str, test_id: str) -> str:
            time.sleep(0.05)
            agent.write(_turn(test_id))  # a turn with no response
            return ""

        agent = _Agent(script)

        summary = _runner(agent, jobs=2, reply_within=0.2, max_errors=2).run(
            _spec(*(Case(trigger=f"t{i}", expect=SAYS_OK) for i in range(6)))
        )

        assert agent.most_in_flight == 2
        assert summary.stop_reason == REPEATED_ERRORS
        assert summary.stopped is not None
        assert summary.stopped.startswith("2 executions in a row failed"), summary.stopped
        assert "agent.reply: turn: no turn record of this test" in summary.tests[0].errors[0]
        assert all(test.status is Status.ERROR for test in summary.tests)


class TestCrash:
    def test_hb8_a_test_that_crashes_highbar_is_an_error_and_the_tests_after_it_are_told(
        self,
    ) -> None:
        b_started = threading.Event()

        def script(agent: _Agent, message: str, test_id: str) -> None:
            if message == "a":
                b_started.wait(1)
                raise _Bug("boom")
            b_started.set()
            time.sleep(0.2)
            agent.write(_turn(test_id))

        reporter = MagicMock()
        runner = _runner(_Agent(script), jobs=2, reporter=reporter)

        with pytest.raises(_Bug):
            runner.run(_spec(Case(trigger="a", expect=SAYS_OK), Case(trigger="b", expect=SAYS_OK)))

        tests = runner.snapshot().tests
        assert [(test.name, test.status) for test in tests] == [
            ("a", Status.ERROR),
            ("b", Status.PASSED),
        ]
        assert reporter.on_test_result.call_count == 2


class TestPythonHooks:
    def test_hb8_python_hooks_called_from_two_threads_leave_stdout_as_it_was(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two tests open their sessions at once: the output of the run is put back."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "slow_hook.py").write_text(
            textwrap.dedent("""\
                import time
                def setup(ctx):
                    time.sleep(0.2)
                    return {}
            """),
            encoding="utf-8",
        )
        hooks = SessionHooks(
            SessionConfig(setup=PythonHook("slow_hook:setup")),
            spec_path="specs/a.highbar.yaml",
            timeout_ms=10_000,
        )
        hooks.load()
        before = sys.stdout

        def setup(name: str) -> None:
            hooks.setup(SessionContext(test_id=name, test_name=name, spec_path="s"))

        first = threading.Thread(target=setup, args=("a",))
        second = threading.Thread(target=setup, args=("b",))
        first.start()
        time.sleep(0.05)
        second.start()
        first.join(5)
        second.join(5)

        assert sys.stdout is before
