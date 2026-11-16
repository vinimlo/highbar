"""HB-14: an agent that answers later, through a queue, gives its reply in its turn record.

Seu AgenteIA's WhatsApp path: the webhook answers 202 and a worker sends the reply
later. With `agent.reply: turn`, the reply is the `response` of the turn record of that
test execution, and highbar waits for it up to agent.timeout. A turn of another test,
or one without a response, is not the reply: waiting ends in `error`, never a pass.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    REPEATED_ERRORS,
    AgentConfig,
    Expect,
    ResponseExpect,
    RunSummary,
    Spec,
    Status,
    Suite,
    TurnRecord,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.runner import TestRunner as Runner
from highbar.engine.schema import SpecError
from tests.unit.engine.fakes import FakeAgent, Script


def _spec(tmp_path: Path, agent: str) -> str:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(
        'agent:\n  endpoint: "http://localhost:8000/chat"\n'
        + agent
        + 'suites:\n  - name: S\n    tests:\n      - trigger: "cancel my training"\n'
        + '        expect: {response: {contains: "days"}}\n',
        encoding="utf-8",
    )
    return str(path)


class TestSpec:
    def test_hb14_the_reply_can_come_from_the_turn_record(self, tmp_path: Path) -> None:
        assert parse_spec(_spec(tmp_path, "  reply: turn\n")).agent.reply == "turn"

    def test_hb14_by_default_it_is_the_http_body(self, tmp_path: Path) -> None:
        assert parse_spec(_spec(tmp_path, "")).agent.reply == "body"

    def test_hb14_another_source_does_not_load(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"agent: reply must be 'body' or 'turn'"):
            parse_spec(_spec(tmp_path, "  reply: trace\n"))

    def test_hb14_a_path_in_the_body_does_not_go_with_it(self, tmp_path: Path) -> None:
        """agent.response reads the HTTP body, which agent.reply: turn does not read."""
        with pytest.raises(SpecError, match=r"agent\.response .* agent\.reply: turn"):
            parse_spec(_spec(tmp_path, "  reply: turn\n  response: data.reply\n"))


DAYS = Expect(response=ResponseExpect(matcher={"contains": "days"}))


def _run(script: Script, *, within: float = 0.5) -> RunSummary:
    agent = FakeAgent(script)
    return Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=MagicMock(),
        settle_time=0,
        reply_within=within,
    ).run(
        Spec(
            agent=AgentConfig(endpoint="http://127.0.0.1:1/chat", reply="turn"),
            suites=[Suite("S", [Case(trigger="cancel my training", expect=DAYS)])],
        )
    )


def _later(response: str | None, *, test: str | None = None) -> Script:
    """Nothing when the message is sent; the turn, with its response, on a later read."""

    def script(message: str, test_id: str) -> tuple[Window, Window]:
        turn = TurnRecord(ts=1.0, context={"test": test or test_id}, response=response)
        return Window(), Window(turns=[turn])

    return script


class TestRunner:
    def test_hb14_the_reply_is_the_response_of_the_turn_that_came_later(self) -> None:
        # The fake's HTTP body is "ok": a pass here can only come from the turn record.
        (test,) = _run(_later("you have 7 days")).tests

        assert test.status is Status.PASSED, test.errors

    def test_hb14_no_turn_in_time_is_an_error(self) -> None:
        (test,) = _run(lambda message, test_id: Window(), within=0.3).tests

        assert test.status is Status.ERROR
        assert any("agent.reply: turn" in e and "0.3s" in e for e in test.errors), test.errors

    def test_hb14_the_turn_of_another_test_is_not_the_reply(self) -> None:
        (test,) = _run(_later("you have 7 days", test="someone-else"), within=0.3).tests

        assert test.status is Status.ERROR, test.errors

    def test_hb14_a_turn_without_a_response_is_not_the_reply(self) -> None:
        (test,) = _run(_later(None), within=0.3).tests

        assert test.status is Status.ERROR, test.errors


class TestMaxErrors:
    """HB-34 with HB-14: a worker that never writes the reply is the same failure each
    time, as an empty body is, so --max-errors stops the run instead of waiting
    agent.timeout for every execution left."""

    def test_hb14_no_reply_in_the_turn_counts_for_max_errors(self) -> None:
        agent = FakeAgent(lambda message, test_id: Window())
        tests = [Case(trigger=f"t{i}", expect=DAYS, runs=4) for i in range(2)]

        summary = Runner(
            adapter=MagicMock(),
            client=agent,  # pyright: ignore[reportArgumentType]
            collector=agent,
            assertions=[FunctionCallsAssertion(), ResponseAssertion()],
            reporter=MagicMock(),
            settle_time=0,
            reply_within=0.05,
            max_errors=3,
        ).run(
            Spec(
                agent=AgentConfig(endpoint="http://127.0.0.1:1/chat", reply="turn"),
                suites=[Suite("S", tests)],
            )
        )

        assert len(agent.sent) == 3
        assert summary.stop_reason == REPEATED_ERRORS
        assert summary.stopped is not None
        assert "no turn record with a response" in summary.stopped, summary.stopped
        assert "agent.timeout (0.05s)" in summary.stopped, summary.stopped

    def test_hb14_a_reply_in_the_turn_starts_the_count_over(self) -> None:
        replies = iter([None, None, "7 days", None, None, "7 days", None, None])

        def script(message: str, test_id: str) -> tuple[Window, Window]:
            response = next(replies)
            if response is None:
                return Window(), Window()
            return Window(), Window(
                turns=[TurnRecord(ts=1.0, context={"test": test_id}, response=response)]
            )

        agent = FakeAgent(script)
        summary = Runner(
            adapter=MagicMock(),
            client=agent,  # pyright: ignore[reportArgumentType]
            collector=agent,
            assertions=[FunctionCallsAssertion(), ResponseAssertion()],
            reporter=MagicMock(),
            settle_time=0,
            reply_within=0.05,
            max_errors=3,
        ).run(
            Spec(
                agent=AgentConfig(endpoint="http://127.0.0.1:1/chat", reply="turn"),
                suites=[Suite("S", [Case(trigger="t", expect=DAYS, runs=8)])],
            )
        )

        assert len(agent.sent) == 8
        assert summary.stop_reason is None
