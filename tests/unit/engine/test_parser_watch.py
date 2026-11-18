"""HB-53: agent.watch, how long a test that forbids a call is watched after its reply."""

from __future__ import annotations

from pathlib import Path

import pytest

from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError
from tests.unit.engine.test_parser_agent import CHECKS_CALLS, _spec

MANAGED = 'start: "python agent.py"\nendpoint: "http://127.0.0.1:8000/chat"\n'


def test_hb53_watch_is_2000_ms_by_default(tmp_path: Path) -> None:
    spec = parse_spec(_spec(tmp_path, MANAGED, CHECKS_CALLS))
    assert spec.agent.watch == 2000


def test_hb53_watch_can_be_set(tmp_path: Path) -> None:
    spec = parse_spec(_spec(tmp_path, MANAGED + "watch: 5000\n", CHECKS_CALLS))
    assert spec.agent.watch == 5000


def test_hb53_a_negative_watch_is_a_spec_error(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="watch"):
        parse_spec(_spec(tmp_path, MANAGED + "watch: -1\n", CHECKS_CALLS))


def test_hb53_which_tests_forbid_a_call(tmp_path: Path) -> None:
    suites = (
        "suites:\n  - name: S\n    tests:\n"
        '      - {trigger: "a", expect: {forbid: [refund]}}\n'
        '      - {trigger: "b", expect: {function_calls: []}}\n'
        '      - {trigger: "c", expect: {exclusive: true, function_calls: [{name: x}]}}\n'
        '      - {trigger: "d", expect: {function_calls: [{name: x}]}}\n'
        '      - {trigger: "e", expect: {response: {contains: "ok"}}}\n'
    )
    spec = parse_spec(_spec(tmp_path, MANAGED, suites))
    forbids = [test.forbids_calls for test in spec.suites[0].tests]
    assert forbids == [True, True, True, False, False]
