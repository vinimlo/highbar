"""HB-3 in the runner: expect.retrieval reads the test's own `retrieval` records, through
the same path as `llm` records (decision E1)."""

from __future__ import annotations

from unittest.mock import MagicMock

from highbar.assertions.function_calls import FunctionCallsAssertion
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Expect,
    ItemRef,
    Retrieval,
    RetrievalExpect,
    RetrievedItem,
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


WARRANTY = RetrievedItem(id="tr-7", source="expanded", chunks=(0, 1))
INCLUDES_IT = Expect(retrieval=RetrievalExpect(includes=(ItemRef("tr-7"),)))
EXCLUDES_IT = Expect(retrieval=RetrievalExpect(excludes=("tr-7",)))


def _tagged(*, owner: str | None = None, late: bool = False) -> Script:
    """Test 0's record with tr-7 lands after its window when ``late``; in time it then has
    a record with another document only."""

    def script(message: str, test_id: str) -> Window | tuple[Window, Window]:
        tag = {"test": test_id}
        turn = TurnRecord(ts=1.0, context=tag)
        record = Retrieval(ts=1.0, context={"test": owner or test_id}, items=(WARRANTY,))
        if late and message == "message 0":
            other = Retrieval(ts=1.0, context=tag, items=(RetrievedItem(id="tr-2"),))
            return Window(turns=[turn], retrievals=[other]), Window(retrievals=[record])
        return Window(turns=[turn], retrievals=[record])

    return script


def test_hb3_the_tests_own_record_is_checked() -> None:
    (passed,) = _run(_tagged(), INCLUDES_IT).tests
    (failed,) = _run(_tagged(), EXCLUDES_IT).tests

    assert passed.status is Status.PASSED, passed.errors
    assert failed.status is Status.FAILED, failed.errors


def test_hb3_another_tests_record_is_not_this_ones() -> None:
    (test,) = _run(_tagged(owner="someone-else"), EXCLUDES_IT).tests

    assert test.status is Status.ERROR, test.errors
    assert any("no `retrieval` record" in e for e in test.errors), test.errors


def test_hb3_a_late_record_judges_its_own_test_again() -> None:
    """Test 0's record lands in test 1's read: it fails test 0, and test 1 has none."""
    first, second = _run(
        _tagged(late=True), EXCLUDES_IT, Expect(response=None, forbid=("x",))
    ).tests

    assert first.status is Status.FAILED, first.errors
    assert any("'tr-7' entered the prompt" in e for e in first.errors), first.errors
    assert second.status is Status.PASSED, second.errors
