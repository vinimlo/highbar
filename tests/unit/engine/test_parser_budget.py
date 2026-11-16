"""`budget` in a spec: where it can go, and what it can hold."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from highbar.domain.models import Budget, Spec
from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError

MANAGED = """\
agent:
  start: "python agent.py"
  endpoint: "http://127.0.0.1:1/chat"
"""
ATTACHED = """\
agent:
  endpoint: "http://127.0.0.1:1/chat"
"""


def _parse(tmp_path: Path, body: str, agent: str = MANAGED) -> Spec:
    path = tmp_path / "x.highbar.yaml"
    path.write_text(agent + textwrap.dedent(body), encoding="utf-8")
    return parse_spec(str(path))


def _with_budget(tmp_path: Path, budget: str) -> Spec:
    return _parse(
        tmp_path,
        f"""\
        suites:
          - name: S
            tests:
              - trigger: "hi"
                expect:
                  budget: {budget}
        """,
    )


class TestWhere:
    def test_ac6_in_the_expect_of_a_test(self, tmp_path: Path) -> None:
        spec = _with_budget(tmp_path, "{max_input_tokens: 6000, max_cost: 0.05}")

        expect = spec.suites[0].tests[0].expect
        assert expect is not None
        assert expect.budget == Budget(max_input_tokens=6000, max_cost=0.05)

    def test_ac6_in_the_expect_of_a_turn(self, tmp_path: Path) -> None:
        spec = _parse(
            tmp_path,
            """\
            suites:
              - name: S
                tests:
                  - turns:
                      - trigger: "hi"
                        expect:
                          budget: {max_cost: 1}
            """,
        )

        (turn,) = spec.suites[0].tests[0].turns or []
        assert turn.expect.budget == Budget(max_cost=1)

    def test_ac6_in_the_expect_of_a_conversation(self, tmp_path: Path) -> None:
        spec = _parse(
            tmp_path,
            """\
            suites:
              - name: S
                tests:
                  - expect:
                      budget: {max_input_tokens: 100}
                    turns:
                      - trigger: "hi"
                      - trigger: "bye"
            """,
        )

        test = spec.suites[0].tests[0]
        assert test.expect is not None
        assert test.expect.budget == Budget(max_input_tokens=100)
        assert test.requires_observation is True

    def test_a_conversation_has_nothing_else_in_its_expect(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="each turn has its own 'expect'"):
            _parse(
                tmp_path,
                """\
                suites:
                  - name: S
                    tests:
                      - expect:
                          budget: {max_cost: 1}
                          forbid: [x]
                        turns:
                          - trigger: "hi"
                            expect: {forbid: [x]}
                """,
            )

    def test_ac10_in_defaults(self, tmp_path: Path) -> None:
        spec = _parse(
            tmp_path,
            """\
            defaults:
              budget: {max_cost: 0.5}
            suites:
              - name: S
                tests:
                  - trigger: "hi"
                    expect: {response: {contains: "hi"}}
            """,
        )

        assert spec.default_budget == Budget(max_cost=0.5)

    def test_a_spec_without_a_budget_has_none(self, tmp_path: Path) -> None:
        spec = _parse(
            tmp_path,
            """\
            suites:
              - name: S
                tests:
                  - trigger: "hi"
                    expect: {response: {contains: "hi"}}
            """,
        )

        assert spec.default_budget is None
        expect = spec.suites[0].tests[0].expect
        assert expect is not None
        assert expect.budget is None
        assert expect.requires_observation is False

    def test_a_budget_alone_asserts_something(self, tmp_path: Path) -> None:
        spec = _with_budget(tmp_path, "{max_cost: 1}")

        assert spec.suites[0].tests[0].requires_observation is True

    @pytest.mark.parametrize("budget", ["{max_input_tokens: 0}", "{max_cost: 0}"])
    def test_zero_is_a_limit(self, tmp_path: Path, budget: str) -> None:
        _with_budget(tmp_path, budget)


class TestInvalid:
    """AC11: the error names the key and the value."""

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            ("-1", "-1"),
            ('"lots"', "'lots'"),
            ("1.5", "1.5"),
            ("true", "True"),
            ("null", "None"),
        ],
    )
    def test_ac11_max_input_tokens(self, tmp_path: Path, value: str, shown: str) -> None:
        with pytest.raises(SpecError) as error:
            _with_budget(tmp_path, f"{{max_input_tokens: {value}}}")

        assert "suites[0].tests[0].expect.budget" in str(error.value)
        assert "max_input_tokens" in str(error.value)
        assert f"got {shown}" in str(error.value)

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            ("-0.01", "-0.01"),
            ('"cheap"', "'cheap'"),
            ("true", "True"),
            ("null", "None"),
            (".inf", "inf"),
            (".nan", "nan"),
        ],
    )
    def test_ac11_max_cost(self, tmp_path: Path, value: str, shown: str) -> None:
        with pytest.raises(SpecError) as error:
            _with_budget(tmp_path, f"{{max_cost: {value}}}")

        assert "max_cost" in str(error.value)
        assert f"got {shown}" in str(error.value)

    def test_ac11_an_unknown_key_suggests_the_closest(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"unknown key 'max_tokens' — did you mean"):
            _with_budget(tmp_path, "{max_tokens: 10}")

    def test_ac11_no_limit_at_all(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="has no limit"):
            _with_budget(tmp_path, "{}")

    def test_an_extension_key_is_no_limit(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="has no limit"):
            _with_budget(tmp_path, "{x-note: why}")

    def test_ac11_not_a_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"budget: must be a mapping"):
            _with_budget(tmp_path, "100")

    def test_ac11_in_defaults_the_error_says_where(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"defaults\.budget: max_cost .* got -1"):
            _parse(
                tmp_path,
                """\
                defaults:
                  budget: {max_cost: -1}
                suites:
                  - name: S
                    tests:
                      - trigger: "hi"
                        expect: {response: {contains: "hi"}}
                """,
            )


class TestAttachMode:
    """A budget reads the turn's record, so an attached agent needs its trace file."""

    def test_a_budget_without_traces_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="needs 'traces'"):
            _parse(
                tmp_path,
                """\
                suites:
                  - name: S
                    tests:
                      - trigger: "hi"
                        expect: {budget: {max_cost: 1}}
                """,
                ATTACHED,
            )

    def test_a_default_budget_without_traces_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match=r"suites\[0\]\.tests\[0\] checks function_calls or"):
            _parse(
                tmp_path,
                """\
                defaults:
                  budget: {max_cost: 1}
                suites:
                  - name: S
                    tests:
                      - trigger: "hi"
                        expect: {response: {contains: "hi"}}
                """,
                ATTACHED,
            )

    def test_a_budget_on_a_conversation_without_traces_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(SpecError, match="needs 'traces'"):
            _parse(
                tmp_path,
                """\
                suites:
                  - name: S
                    tests:
                      - expect: {budget: {max_cost: 1}}
                        turns:
                          - trigger: "hi"
                            expect: {response: {contains: "hi"}}
                """,
                ATTACHED,
            )

    def test_with_traces_it_parses(self, tmp_path: Path) -> None:
        _parse(
            tmp_path,
            """\
            suites:
              - name: S
                tests:
                  - trigger: "hi"
                    expect: {budget: {max_cost: 1}}
            """,
            ATTACHED + '  traces: "traces.jsonl"\n',
        )
