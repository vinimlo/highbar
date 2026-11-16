"""`returns`: the whole value a tool returned, for a return that is not an object (HB-37)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError


def _spec(tmp_path: Path, entry: str) -> str:
    spec = tmp_path / "test.highbar.yaml"
    spec.write_text(
        textwrap.dedent(
            """
            agent:
              start: "true"
              endpoint: "http://localhost:8000/chat"
            suites:
              - name: Docs
                tests:
                  - trigger: "find the manual"
                    expect:
                      function_calls:
                        - name: search_docs
            """
        )
        + textwrap.indent(entry, " " * 14)
        + "\n"
    )
    return str(spec)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("returns: {len_lte: 3}", {"len_lte": 3}),
        ("returns: [a, b]", ["a", "b"]),
        ('returns: "done"', "done"),
        ("returns: {all: {source: retrieved}}", {"all": {"source": "retrieved"}}),
        ("returns: {equals: null}", {"equals": None}),
        ("returns: 0", 0),
    ],
)
def test_hb37_returns_loads_any_expected_value(
    tmp_path: Path, entry: str, expected: object
) -> None:
    call = parse_spec(_spec(tmp_path, entry)).suites[0].tests[0].expect.function_calls[0]
    assert call.returns == expected


def test_hb37_no_returns_is_none(tmp_path: Path) -> None:
    call = parse_spec(_spec(tmp_path, "params: {q: x}")).suites[0].tests[0].expect.function_calls[0]
    assert call.returns is None


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("returns: null", "returns: null checks nothing"),
        ("returns: {len_lte: 3, x: 1}", "mixes matcher keys"),
        ('returns: {icontain: "x"}', "did you mean 'icontains'"),
        ('returns: "{{previous.response}}"', "templates are only resolved in triggers"),
    ],
)
def test_hb37_a_wrong_returns_does_not_load(tmp_path: Path, entry: str, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        parse_spec(_spec(tmp_path, entry))
