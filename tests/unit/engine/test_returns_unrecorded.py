"""Review of #74: a call whose return was never recorded cannot pass `returns` (HB-37).

hb.record() used to write "result": null whether or not a return was given, and the
collector read a missing key as None, so `returns: {equals: null}` held on a call
whose return nobody saw. A return not recorded leaves `returns` unchecked: `error`,
unless something else already failed the test.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from highbar.adapters.file_collector import FileTraceCollector
from highbar.assertions.function_calls import FunctionCallsAssertion, satisfies
from highbar.assertions.response import ResponseAssertion
from highbar.domain.models import (
    AgentConfig,
    Expect,
    FunctionCallExpect,
    RunSummary,
    Spec,
    Status,
    Suite,
    Trace,
    TurnRecord,
    Window,
)
from highbar.domain.models import Test as Case
from highbar.engine.runner import TestRunner as Runner
from highbar.sdk.core import Highbar
from tests.unit.engine.fakes import FakeAgent, Script


def _run(script: Script, expect: Expect) -> RunSummary:
    agent = FakeAgent(script)
    return Runner(
        adapter=MagicMock(),
        client=agent,  # pyright: ignore[reportArgumentType]
        collector=agent,
        assertions=[FunctionCallsAssertion(), ResponseAssertion()],
        reporter=MagicMock(),
        settle_time=0,
    ).run(
        Spec(
            agent=AgentConfig(endpoint="http://127.0.0.1:1/chat"),
            suites=[Suite("S", [Case(trigger="find it", expect=expect)])],
        )
    )


def _calls(*calls: Trace) -> Script:
    def script(message: str, test_id: str) -> Window:
        tag = {"test": test_id}
        return Window(
            turns=[TurnRecord(ts=1.0, context=tag)],
            tools=[Trace(**{**c.__dict__, "context": tag}) for c in calls],
        )

    return script


def _call(fn: str = "search", *, result: object = None, has_result: bool = True) -> Trace:
    return Trace(fn=fn, args={}, result=result, started_at=1.0, ended_at=1.1, has_result=has_result)


NULL_RETURN = Expect(function_calls=[FunctionCallExpect(name="search", returns={"equals": None})])


def test_review_74_an_unrecorded_return_never_satisfies_returns() -> None:
    assert not satisfies(NULL_RETURN.function_calls[0], _call(has_result=False))  # type: ignore[index]
    assert satisfies(NULL_RETURN.function_calls[0], _call(result=None))  # type: ignore[index]


def test_review_74_returns_on_an_unrecorded_return_is_an_error() -> None:
    (test,) = _run(_calls(_call(has_result=False)), NULL_RETURN).tests
    assert test.status is Status.ERROR, test.errors
    assert any("return of 'search' was not recorded" in e for e in test.errors), test.errors


def test_review_74_a_recorded_null_return_passes() -> None:
    (test,) = _run(_calls(_call(result=None)), NULL_RETURN).tests
    assert test.status is Status.PASSED, test.errors


def test_review_74_a_failure_elsewhere_stays_failed() -> None:
    expect = Expect(
        function_calls=[
            FunctionCallExpect(name="search", returns={"equals": None}),
            FunctionCallExpect(name="book"),
        ]
    )
    (test,) = _run(_calls(_call(has_result=False)), expect).tests
    assert test.status is Status.FAILED, test.errors


def test_review_74_hb_record_without_result_writes_no_result(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    hb = Highbar(traces=str(path))
    hb.record("search", {"q": "x"})
    hb.record("search", {"q": "y"}, result=None)
    lines = [json.loads(line) for line in path.read_text().splitlines() if '"tool"' in line]
    assert "result" not in lines[0]
    assert lines[1]["result"] is None

    window = FileTraceCollector(path).collect(since=0)
    assert [t.has_result for t in window.tools] == [False, True]
