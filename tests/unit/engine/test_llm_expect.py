"""HB-11 in the runner: expect.llm reads the test's own `llm` records.

Decisions on the card: the records go by correlation like tool calls (E1); none is an
`error`, never a pass (E2); a late one judges its own test again, as a late call does.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Expect,
    FunctionCallExpect,
    LlmCall,
    LlmExpect,
    RunSummary,
    Spec,
    Status,
    Suite,
    TurnRecord,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.runner import TestRunner as Runner
from tests.unit.engine.fakes import FakeAgent, Script


def _run(script: Script, *expects: Expect) -> RunSummary:
    agent = FakeAgent(script)
    tests = [Case(trigger=f"message {n}", expect=e) for n, e in enumerate(expects)]
    return Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=MagicMock(),
        settle_time=0,
    ).run(Spec(agent=AgentConfig(endpoint="http://127.0.0.1:1/chat"), suites=[Suite("S", tests)]))


def _with(*roles: tuple[str, ...]) -> Script:
    """A turn and one `llm` record per call, each with these roles, tagged with the test."""

    def script(message: str, test_id: str) -> Window:
        tag = {"test": test_id}
        return Window(
            turns=[TurnRecord(ts=1.0, context=tag)],
            llms=[LlmCall(ts=1.0, context=tag, model="m", roles=r) for r in roles],
        )

    return script


SYSTEM_LAST = Expect(llm=LlmExpect(last_role="system"))


def test_hb11_the_tests_own_calls_are_checked() -> None:
    (passed,) = _run(_with(("user", "system")), SYSTEM_LAST).tests
    (failed,) = _run(_with(("user", "system"), ("system", "user")), SYSTEM_LAST).tests

    assert passed.status is Status.PASSED, passed.errors
    assert failed.status is Status.FAILED
    assert any("call 2 ended on a 'user' message" in e for e in failed.errors), failed.errors


def test_hb11_no_llm_record_is_an_error_not_a_pass() -> None:
    (test,) = _run(_with(), SYSTEM_LAST).tests

    assert test.status is Status.ERROR
    assert any("no `llm` record" in e for e in test.errors), test.errors


def test_hb11_another_tests_calls_are_not_this_ones() -> None:
    def script(message: str, test_id: str) -> Window:
        return Window(
            turns=[TurnRecord(ts=1.0, context={"test": test_id})],
            llms=[LlmCall(ts=1.0, context={"test": "someone-else"}, roles=("user", "system"))],
        )

    (test,) = _run(script, SYSTEM_LAST).tests

    assert test.status is Status.ERROR, test.errors


def test_hb11_a_late_call_judges_its_own_test_again() -> None:
    """The fallback call lands after test 0's window: it still fails test 0, not test 1."""

    def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
        tag = {"test": test_id}
        now = Window(
            turns=[TurnRecord(ts=1.0, context=tag)],
            llms=[LlmCall(ts=1.0, context=tag, model="gpt", roles=("system",))],
        )
        if message != "message 0":
            return now
        late = Window(llms=[LlmCall(ts=2.0, context=tag, model="b", fallback_from="gpt")])
        return now, late

    first, second = _run(
        script, Expect(llm=LlmExpect(fallback=False)), Expect(llm=LlmExpect(fallback=False))
    ).tests

    assert first.status is Status.FAILED, first.errors
    assert any("a fallback happened" in e for e in first.errors), first.errors
    assert second.status is Status.PASSED, second.errors


def test_hb11_a_test_that_failed_otherwise_stays_failed_without_llm_records() -> None:
    expect = Expect(
        function_calls=[FunctionCallExpect(name="cancel_order")],
        llm=LlmExpect(max_calls=1),
    )

    (test,) = _run(_with(), expect).tests

    assert test.status is Status.FAILED, test.errors
