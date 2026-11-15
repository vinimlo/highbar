"""HB-12 end to end: ``expect.check`` in a real run of the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_report import _free_port, _Project

CHECKS = """\
import pathlib

def state_written(ctx):
    pathlib.Path("seen.txt").write_text(f"{ctx.message}|{ctx.response}|{len(ctx.turns)}")
    return []

def state_missing(ctx):
    return [f"no order row for {ctx.message!r}"]
"""


def _spec(project: _Project, function: str) -> str:
    (project.root / "evals_checks.py").write_text(CHECKS, encoding="utf-8")
    text = _Project.agent(_free_port()) + (
        "suites:\n  - name: S\n    tests:\n"
        '      - trigger: "place order 1042"\n'
        f'        expect: {{check: {{python: "evals_checks:{function}"}}}}\n'
    )
    (project.root / "agent.highbar.yaml").write_text(text, encoding="utf-8")
    return "agent.highbar.yaml"


@pytest.fixture
def project(tmp_path: Path) -> _Project:
    return _Project(tmp_path)


@pytest.mark.timeout(30)
def test_hb12_a_check_that_finds_nothing_passes(project: _Project) -> None:
    result = project.run(_spec(project, "state_written"))

    assert result.returncode == 0, result.stdout + result.stderr
    message, response, turns = (project.root / "seen.txt").read_text().split("|")
    assert message == "place order 1042"
    assert response
    assert turns == "1"


@pytest.mark.timeout(30)
def test_hb12_its_failure_is_the_tests(project: _Project) -> None:
    result = project.run(_spec(project, "state_missing"))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "no order row for 'place order 1042'" in result.stdout
    (test,) = project.data()["specs"][0]["tests"]
    assert test["status"] == "failed"
