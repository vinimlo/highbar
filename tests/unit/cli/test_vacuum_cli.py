"""`highbar vacuum` from the command line (HB-47)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from typer.testing import CliRunner

from highbar.cli.main import app

runner = CliRunner()

HEAD = """\
agent:
  start: "true"
  endpoint: "http://localhost:8000/chat"
suites:
  - name: Orders
    tests:
"""


def _spec(tmp_path: Path, tests: str) -> Path:
    path = tmp_path / "orders.highbar.yaml"
    path.write_text(HEAD + textwrap.indent(textwrap.dedent(tests), " " * 6))
    return path


STRONG = """
- name: looks the order up
  trigger: "Where is order 1042?"
  expect:
    function_calls:
      - name: get_order
        params: {order_id: "1042"}
    response: {contains: "in transit"}
"""

WEAK = """
- name: never says error
  trigger: "Where is order 1042?"
  expect:
    response: {not_contains: "error"}
"""


def test_hb47_a_vacuous_test_is_named_with_who_passes_it_and_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["vacuum", str(_spec(tmp_path, STRONG + WEAK))])
    assert result.exit_code == 1, result.output
    assert "never says error" in result.output
    assert "passes on: silent" in result.output
    assert "1 vacuous of 2" in result.output


def test_hb47_strong_tests_exit_0(tmp_path: Path) -> None:
    result = runner.invoke(app, ["vacuum", str(_spec(tmp_path, STRONG))])
    assert result.exit_code == 0, result.output
    assert "0 vacuous of 1" in result.output


def test_hb47_an_invalid_spec_exits_2(tmp_path: Path) -> None:
    path = tmp_path / "bad.highbar.yaml"
    path.write_text("agent: {}\n")
    result = runner.invoke(app, ["vacuum", str(path)])
    assert result.exit_code == 2, result.output


def test_hb47_a_test_it_cannot_judge_is_said_apart(tmp_path: Path) -> None:
    tests = """
    - name: checks the database
      trigger: "cancel 1042"
      expect:
        check: {python: "checks:cancelled"}
    """
    result = runner.invoke(app, ["vacuum", str(_spec(tmp_path, tests))])
    assert result.exit_code == 0, result.output
    assert "not judged" in result.output and "expect.check" in result.output
