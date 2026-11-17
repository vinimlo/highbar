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
from highbar.engine.runner import _too_slow
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

    def test_hb18_an_integer_too_large_for_a_float_is_a_spec_error(self, tmp_path: Path) -> None:
        """Review of fbbf029: 309 digits or more crashed the parse with an OverflowError
        from ``float()``, and ``highbar validate`` exited 1 with a traceback."""
        huge = "1" + "0" * 400

        with pytest.raises(
            SpecError,
            match=r"suites\[0\]\.tests\[0\]\.expect: max_duration_ms must be a number of "
            r"milliseconds above 0, got an integer of more than 308 digits$",
        ):
            parse_spec(_spec(tmp_path, MANAGED, _suites(f"{{max_duration_ms: {huge}}}")))

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


class TestAfterTheReview:
    """revisao-highbar's suggestions on f79ac17: the clock, the edge, and the hash."""

    def test_hb18_the_wall_clock_moving_back_does_not_make_a_reply_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = time.time

        def script(message: str, test_id: str) -> Window:
            time.sleep(0.08)
            # NTP or a resumed VM moves the wall clock back an hour during the request.
            monkeypatch.setattr(time, "time", lambda: real() - 3600)
            return observed(message, test_id)

        test = Case(trigger="t", expect=Expect(max_duration_ms=10))

        (outcome,) = _run([test], FakeAgent(script)).tests

        assert outcome.status is Status.FAILED

    @pytest.mark.parametrize(
        ("limit", "took", "message"),
        [
            (100, 100.4, "the reply took 101 ms, over max_duration_ms 100"),
            (1234.5678, 2000, "the reply took 2000 ms, over max_duration_ms 1234.5678"),
            (1500000, 1600000.2, "the reply took 1600001 ms, over max_duration_ms 1500000"),
        ],
    )
    def test_hb18_what_goes_over_reads_as_over(
        self, limit: float, took: float, message: str
    ) -> None:
        assert _too_slow(limit, took) == [message]

    def test_hb18_1000_and_1000_0_are_the_same_limit(self, tmp_path: Path) -> None:
        def sha(value: object) -> str:
            return Case(trigger="a", expect=Expect(max_duration_ms=value)).definition_sha256  # type: ignore[arg-type]

        assert sha(1000) == sha(1000.0)
        whole = parse_spec(_spec(tmp_path, MANAGED, _suites("{max_duration_ms: 1000}")))
        point = parse_spec(_spec(tmp_path, MANAGED, _suites("{max_duration_ms: 1000.0}")))
        assert (
            whole.suites[0].tests[0].definition_sha256 == point.suites[0].tests[0].definition_sha256
        )
