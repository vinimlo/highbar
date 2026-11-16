"""HB-43: an agent highbar started that is gone is not the one that answered.

The port is free when highbar checks it, and the agent's SDK writes its hello at import.
If another process binds the port first, the agent dies on its own bind, and that
process answers every message. The agent's trace file then stays empty, and in managed
mode an empty window with nothing tagged is the test's: a guardrail passed with nothing
verified (review of #57). An execution with no record of its own rests on the agent
being the one that answered, so once highbar sees the agent gone with something else on
its port, every such execution is unverified, and so is each turn from there on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from highbar.adapters.subprocess_adapter import AgentExit
from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Expect,
    ResponseExpect,
    RunSummary,
    Spec,
    Status,
    Suite,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.observation import AgentNotObservedError
from highbar.engine.runner import TestRunner as Runner
from tests.unit.engine.fakes import FakeAgent, observed

GUARDRAIL = Expect(function_calls=[])
SAYS_OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))
IMPOSTOR = AgentExit(code=1, port_answers=True)
CRASHED = AgentExit(code=1, port_answers=False)


def _records_nothing(message: str, test_id: str) -> Window:
    return Window()


@dataclass
class _Adapter:
    """The agent's process: alive for the first ``alive`` looks, then ``then``."""

    alive: int
    then: AgentExit
    looks: int = 0
    log_path: Any = "agent.log"

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def exited(self) -> AgentExit | None:
        self.looks += 1
        return None if self.looks <= self.alive else self.then


def _run(tests: list[Case], agent: FakeAgent, adapter: Any, reporter: Any = None) -> RunSummary:
    return Runner(
        adapter=adapter,
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=reporter or MagicMock(),
        settle_time=0,
    ).run(
        Spec(
            agent=AgentConfig(start="agent", endpoint="http://127.0.0.1:1/chat"),
            suites=[Suite(name="S", tests=tests)],
        )
    )


def _guardrails(n: int) -> list[Case]:
    return [Case(trigger=f"close account {i}", expect=GUARDRAIL) for i in range(n)]


class TestGone:
    def test_hb43_a_guardrail_on_an_empty_window_is_unverified_once_the_agent_is_gone(
        self,
    ) -> None:
        """The reviewer's shape: the hello is in, then another process answers."""
        reporter = MagicMock()
        # Looks: the hello, then a turn each. The agent is seen gone at the second turn.
        summary = _run(
            _guardrails(2),
            FakeAgent(_records_nothing),
            _Adapter(alive=2, then=IMPOSTOR),
            reporter,
        )

        first, second = summary.tests
        assert first.status is Status.ERROR
        assert second.status is Status.ERROR
        assert "the agent highbar started exited with code 1" in second.errors[0]
        # Said once: the turn's own result, not a finding about it too.
        assert len(second.errors) == 1
        assert "the agent highbar started exited with code 1" in first.errors[-1]
        # The first had been reported as passed: the warning says it is not.
        warnings = " ".join(c.args[0] for c in reporter.on_warning.call_args_list)
        assert "close account 0" in warnings

    def test_hb43_an_agent_gone_by_the_end_unverifies_what_rested_on_it(self) -> None:
        summary = _run(
            _guardrails(1), FakeAgent(_records_nothing), _Adapter(alive=2, then=IMPOSTOR)
        )

        (test,) = summary.tests
        assert test.status is Status.ERROR
        assert "the agent highbar started exited with code 1" in test.errors[-1]

    def test_hb43_a_reply_checked_on_no_record_rests_on_the_agent_too(self) -> None:
        """A response-only test reads the reply: another process's reply is not the agent's."""
        summary = _run(
            [Case(trigger="hi", expect=SAYS_OK)],
            FakeAgent(_records_nothing),
            _Adapter(alive=1, then=IMPOSTOR),
        )

        assert summary.tests[0].status is Status.ERROR

    def test_hb43_an_execution_with_a_record_of_its_own_stands(self) -> None:
        """Its record is in the agent's own file: the agent handled that message."""
        summary = _run(
            [Case(trigger="hi", expect=GUARDRAIL)],
            FakeAgent(observed),
            _Adapter(alive=2, then=IMPOSTOR),
        )

        assert summary.tests[0].status is Status.PASSED

    def test_hb43_an_agent_that_crashed_with_nothing_on_its_port_leaves_what_it_answered(
        self,
    ) -> None:
        """Nothing else answers: it answered until it crashed, and a warning says so."""
        reporter = MagicMock()
        summary = _run(
            _guardrails(1),
            FakeAgent(_records_nothing),
            _Adapter(alive=2, then=CRASHED),
            reporter,
        )

        assert summary.tests[0].status is Status.PASSED
        warnings = " ".join(c.args[0] for c in reporter.on_warning.call_args_list)
        assert "the agent highbar started exited with code 1" in warnings

    def test_hb43_an_agent_gone_right_after_its_hello_stops_the_run(self) -> None:
        with pytest.raises(AgentNotObservedError, match="exited with code 1"):
            _run(_guardrails(1), FakeAgent(_records_nothing), _Adapter(alive=0, then=IMPOSTOR))

    def test_hb43_an_attached_agent_is_not_looked_at(self) -> None:
        """highbar did not start it: there is no process of its own to look at."""
        adapter = _Adapter(alive=0, then=IMPOSTOR)
        agent = FakeAgent(observed)
        Runner(
            adapter=adapter,  # pyright: ignore[reportArgumentType]
            client=agent,  # pyright: ignore[reportArgumentType]
            collector=agent,
            assertions=[ResponseAssertion()],
            reporter=MagicMock(),
            settle_time=0,
            managed=False,
        ).run(
            Spec(
                agent=AgentConfig(endpoint="http://127.0.0.1:1/chat"),
                suites=[Suite(name="S", tests=[Case(trigger="hi", expect=SAYS_OK)])],
            )
        )

        assert adapter.looks == 0
