"""HB-11: expect.llm in the spec."""

from __future__ import annotations

from pathlib import Path

import pytest

from highbar.domain.models import LlmExpect
from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError


def _spec(tmp_path: Path, llm: str) -> str:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(
        'agent:\n  endpoint: "http://localhost:8000/chat"\n  traces: t.jsonl\n'
        'suites:\n  - name: S\n    tests:\n      - trigger: "hi"\n'
        f"        expect:\n          llm: {llm}\n",
        encoding="utf-8",
    )
    return str(path)


def test_hb11_expect_llm_is_parsed(tmp_path: Path) -> None:
    spec = parse_spec(
        _spec(tmp_path, "{model: deepseek-flash, fallback: true, last_role: system, max_calls: 3}")
    )

    expect = spec.suites[0].tests[0].expect
    assert expect.llm == LlmExpect(
        model={"equals": "deepseek-flash"}, fallback=True, last_role="system", max_calls=3
    )
    assert expect.requires_observation


def test_hb11_model_takes_a_text_matcher(tmp_path: Path) -> None:
    spec = parse_spec(_spec(tmp_path, '{model: {matches: "^gpt-4o"}}'))

    assert spec.suites[0].tests[0].expect.llm == LlmExpect(model={"matches": "^gpt-4o"})


@pytest.mark.parametrize(
    ("llm", "message"),
    [
        ("{fallbak: true}", r"unknown key 'fallbak'.*did you mean 'fallback'"),
        ("{fallback: no-way}", r"fallback must be true or false"),
        ("{max_calls: 0}", r"max_calls must be a whole number of at least 1"),
        ("{last_role: ''}", r"last_role must be a role name"),
        ("{}", r"llm checks nothing"),
        ('{model: {matches: "("}}', r"invalid regex"),
    ],
)
def test_hb11_a_wrong_llm_check_does_not_load(tmp_path: Path, llm: str, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        parse_spec(_spec(tmp_path, llm))
