"""HB-18: how long the agent may take to answer a message, ``expect.max_duration_ms``.

A latency regression must fail the test, as a budget's does: the reply's time is the
one highbar measures from the request it sends to the reply it reads, with no session
setup and no settle time in it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from highbar.domain.models import Expect, ResponseExpect, Status, Turn, Window
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError
from tests.unit.engine.fakes import FakeAgent, observed
from tests.unit.engine.test_budget import _run
from tests.unit.engine.test_parser_agent import MANAGED, _spec

SAYS_OK = ResponseExpect(matcher={"contains": "ok"})


def _suites(expect: str) -> str:
    return f'suites:\n  - name: S\n    tests:\n      - trigger: "hi"\n        expect: {expect}\n'


def _slow(seconds: float) -> FakeAgent:
    def script(message: str, test_id: str) -> Window:
        time.sleep(seconds)
        return observed(message, test_id)

    return FakeAgent(script)


class TestTheSpec:
    def test_hb18_a_test_sets_how_long_its_reply_may_take(self, tmp_path: Path) -> None:
        spec = parse_spec(_spec(tmp_path, MANAGED, _suites("{max_duration_ms: 1500}")))

        assert spec.suites[0].tests[0].expect == Expect(max_duration_ms=1500)

    def test_hb18_a_turn_sets_its_own(self, tmp_path: Path) -> None:
        suites = (
            "suites:\n  - name: S\n    tests:\n      - turns:\n"
            '          - trigger: "hi"\n            expect: {max_duration_ms: 800}\n'
            '          - trigger: "and?"\n            expect: {response: {contains: "ok"}}\n'
        )

        (test,) = parse_spec(_spec(tmp_path, MANAGED, suites)).suites[0].tests

        assert test.turns is not None
        assert [turn.expect.max_duration_ms for turn in test.turns] == [800, None]

    @pytest.mark.parametrize("value", ["0", "-5", "true", "fast", ".inf", ".nan", "null"])
    def test_hb18_it_is_a_number_of_milliseconds_above_0(self, tmp_path: Path, value: str) -> None:
        with pytest.raises(
            SpecError,
            match=r"suites\[0\]\.tests\[0\]\.expect: max_duration_ms must be a number of "
            r"milliseconds above 0",
        ):
            parse_spec(_spec(tmp_path, MANAGED, _suites(f"{{max_duration_ms: {value}}}")))

    def test_hb18_a_conversation_sets_it_per_turn(self, tmp_path: Path) -> None:
        suites = (
            "suites:\n  - name: S\n    tests:\n      - expect: {max_duration_ms: 800}\n"
            "        turns:\n"
            '          - trigger: "hi"\n            expect: {response: {contains: "ok"}}\n'
        )

        with pytest.raises(SpecError, match=r"the test's can only hold a 'budget'"):
            parse_spec(_spec(tmp_path, MANAGED, suites))

    def test_hb18_it_changes_what_a_test_measures_only_when_set(self) -> None:
        plain = Case(trigger="a", expect=Expect(response=SAYS_OK))
        timed = Case(trigger="a", expect=Expect(response=SAYS_OK, max_duration_ms=1000))

        assert Case(trigger="a", expect=Expect(response=SAYS_OK)).definition_sha256 == (
            plain.definition_sha256
        )
        assert timed.definition_sha256 != plain.definition_sha256


class TestTheRun:
    def test_hb18_a_reply_slower_than_its_limit_fails(self) -> None:
        test = Case(trigger="t", expect=Expect(response=SAYS_OK, max_duration_ms=10))

        (outcome,) = _run([test], _slow(0.08)).tests

        assert outcome.status is Status.FAILED
        (error,) = outcome.errors
        assert error.startswith("the reply took ")
        assert error.endswith(" ms, over max_duration_ms 10")
        took = int(error.removeprefix("the reply took ").split(" ")[0])
        assert took >= 80

    def test_hb18_a_reply_within_its_limit_passes(self) -> None:
        test = Case(trigger="t", expect=Expect(response=SAYS_OK, max_duration_ms=5000))

        (outcome,) = _run([test], _slow(0.01)).tests

        assert outcome.status is Status.PASSED

    def test_hb18_a_limit_alone_is_checked(self) -> None:
        """Nothing else to check: the reply's time is enough to fail a test."""
        (outcome,) = _run([Case(trigger="t", expect=Expect(max_duration_ms=10))], _slow(0.08)).tests

        assert outcome.status is Status.FAILED

    def test_hb18_each_turn_is_held_to_its_own(self) -> None:
        turns = [
            Turn(trigger="fast", expect=Expect(response=SAYS_OK, max_duration_ms=5000)),
            Turn(trigger="slow", expect=Expect(response=SAYS_OK, max_duration_ms=10)),
        ]

        (outcome,) = _run([Case(turns=turns)], _slow(0.08)).tests

        assert outcome.status is Status.FAILED
        assert any("over max_duration_ms 10" in error for error in outcome.errors)
        assert not any("max_duration_ms 5000" in error for error in outcome.errors)

    def test_hb18_a_failed_check_is_still_a_failure_with_it(self) -> None:
        expect = Expect(response=ResponseExpect(matcher={"contains": "absent"}), max_duration_ms=10)

        (outcome,) = _run([Case(trigger="t", expect=expect)], _slow(0.08)).tests

        assert outcome.status is Status.FAILED
        assert len(outcome.errors) == 2
